from __future__ import annotations

from dataclasses import dataclass

import feedparser
import requests

from config import Config
from dates import format_date
from gemini import extract_articles


@dataclass
class Article:
    link: str
    title: str
    pub_date: str


def fetch_rss_items(url: str) -> list[Article]:
    parsed = feedparser.parse(url)
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
    response = requests.get(
        url,
        headers={"Cookie": cfg.scrape_cookie_header},
        timeout=30,
    )
    response.raise_for_status()

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
