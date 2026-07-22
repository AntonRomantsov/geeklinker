from __future__ import annotations

import time
from dataclasses import dataclass

import feedparser
import requests

from browser_headers import BROWSER_HEADERS
from config import Config
from dates import format_date
from gemini import extract_articles

# Транзиентные HTTP-ошибки (в т.ч. 403 - на практике у некоторых сайтов это
# кратковременный анти-бот/rate-limit, а не постоянная блокировка) имеет смысл повторить.
RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
RETRY_BACKOFF_SECONDS = (2, 5, 10)


@dataclass
class Article:
    link: str
    title: str
    pub_date: str


def _get_with_retry(url: str, headers: dict | None = None) -> requests.Response:
    merged_headers = {**BROWSER_HEADERS, **(headers or {})}
    attempts = len(RETRY_BACKOFF_SECONDS) + 1
    for attempt in range(attempts):
        response = requests.get(url, headers=merged_headers, timeout=30)
        if response.status_code not in RETRYABLE_STATUS_CODES or attempt == attempts - 1:
            response.raise_for_status()
            return response
        time.sleep(RETRY_BACKOFF_SECONDS[attempt])


def fetch_rss_items(url: str) -> list[Article]:
    parsed = feedparser.parse(url, request_headers=BROWSER_HEADERS)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"не удалось разобрать RSS: {parsed.bozo_exception}")

    items = []
    for entry in reversed(parsed.entries):
        link = entry.get("link", "").strip()
        if not link:
            continue
        struct = entry.get("published_parsed") or entry.get("updated_parsed")
        raw_date = entry.get("published", "") or entry.get("updated", "")
        items.append(
            Article(
                link=link,
                title=entry.get("title", "").strip(),
                pub_date=format_date(raw_date, struct),
            )
        )
    return items


def fetch_scrape_items(url: str, cfg: Config) -> list[Article]:
    response = _get_with_retry(url, headers={"Cookie": cfg.scrape_cookie_header})
    extracted = extract_articles(response.text, cfg)

    items = []
    for entry in reversed(extracted):
        link = (entry.get("url") or "").strip()
        if not link:
            continue
        items.append(
            Article(
                link=link,
                title=(entry.get("title") or "").strip(),
                pub_date=format_date(entry.get("pubDate", "")),
            )
        )
    return items
