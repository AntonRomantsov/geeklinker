from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests

FEED_LINK_RE = re.compile(
    r'<link[^>]+rel=["\']alternate["\'][^>]*type=["\'](?:application/rss\+xml|application/atom\+xml)["\'][^>]*>',
    re.IGNORECASE,
)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def discover_feed_url(page_url: str) -> str | None:
    try:
        response = requests.get(page_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return None

    match = FEED_LINK_RE.search(response.text)
    if not match:
        return None
    href_match = HREF_RE.search(match.group(0))
    if not href_match:
        return None
    return urljoin(page_url, href_match.group(1))


def _pattern_to_regex(pattern: str) -> re.Pattern:
    # Поддержка wildcard-расширения robots.txt (* и $), которое НЕ понимает urllib.robotparser.
    has_end_anchor = pattern.endswith("$")
    if has_end_anchor:
        pattern = pattern[:-1]
    escaped = ".*".join(re.escape(part) for part in pattern.split("*"))
    if has_end_anchor:
        escaped += "$"
    return re.compile("^" + escaped)


def _parse_wildcard_group(text: str) -> list[tuple[str, str]]:
    """Возвращает список (allow|disallow, pattern) правил для группы User-agent: *."""
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    current_agents: list[str] = []
    current_rules: list[tuple[str, str]] = []

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()

        if field == "user-agent":
            if current_rules:
                groups.append((current_agents, current_rules))
                current_agents, current_rules = [], []
            current_agents.append(value.lower())
        elif field in ("disallow", "allow") and current_agents:
            current_rules.append((field, value))

    if current_agents or current_rules:
        groups.append((current_agents, current_rules))

    rules: list[tuple[str, str]] = []
    for agents, group_rules in groups:
        if "*" in agents:
            rules.extend(group_rules)
    return rules


def is_allowed_by_robots(url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    try:
        response = requests.get(robots_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    except requests.exceptions.RequestException:
        return False  # не удалось проверить - консервативно считаем запрещённым

    if response.status_code >= 500:
        return False
    if response.status_code >= 400:
        return True  # robots.txt отсутствует - по умолчанию всё разрешено

    rules = _parse_wildcard_group(response.text)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    best_length = -1
    allowed = True
    for directive, pattern in rules:
        if not pattern:
            continue  # пустой Disallow означает "ничего не запрещено"
        if _pattern_to_regex(pattern).match(path) and len(pattern) > best_length:
            best_length = len(pattern)
            allowed = directive == "allow"
    return allowed
