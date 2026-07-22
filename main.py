from __future__ import annotations

import functions_framework

from bot_webhook import telegram_webhook  # noqa: F401 - должна быть видна в main.py для functions-framework
from config import Config
from feeds import fetch_rss_items, fetch_scrape_items
from notify import send_blocked_source_warning, send_error_alert, send_new_article
from rss_discovery import discover_feed_url, is_allowed_by_robots
from sheets import SheetsClient

BLOCKED_TYPE = "!blocked"


def execute() -> str:
    cfg = Config.from_env()
    sheets = SheetsClient(cfg)

    dedup_links = sheets.load_dedup_links()
    target_sheet_cache: dict[str, set[str]] = {}
    errors: list[str] = []
    new_articles = 0

    for source in sheets.load_active_sources():
        try:
            if source.source_type == BLOCKED_TYPE:
                send_blocked_source_warning(cfg, source.site_name, source.url)
                continue

            if source.source_type not in ("rss", "scrape"):
                feed_url = discover_feed_url(source.url)
                if feed_url and is_allowed_by_robots(feed_url):
                    source.url = feed_url
                    source.source_type = "rss"
                elif is_allowed_by_robots(source.url):
                    source.source_type = "scrape"
                else:
                    source.source_type = BLOCKED_TYPE
                    sheets.update_source_type(source.row_number, source.url, source.source_type)
                    send_blocked_source_warning(cfg, source.site_name, source.url)
                    continue
                sheets.update_source_type(source.row_number, source.url, source.source_type)

            target_links = target_sheet_cache.setdefault(
                source.target_sheet, sheets.load_sheet_links(source.target_sheet)
            )

            if source.source_type == "rss":
                items = fetch_rss_items(source.url)
            else:
                items = fetch_scrape_items(source.url, cfg)

            for item in items:
                if item.link in dedup_links or item.link in target_links:
                    continue

                sheets.append_dedup(item.link, item.pub_date)
                sheets.append_article(source.target_sheet, source.site_name, item.link, item.title, item.pub_date)
                send_new_article(cfg, source.site_name, source.target_sheet, item)

                dedup_links.add(item.link)
                target_links.add(item.link)
                new_articles += 1

        except Exception as exc:  # noqa: BLE001 - собираем ошибку по источнику и продолжаем остальные
            errors.append(f"{source.site_name} ({source.url}): {exc}")

    if errors:
        send_error_alert(cfg, errors)

    summary = f"Готово. Новых статей: {new_articles}. Ошибок: {len(errors)}"
    if errors:
        raise RuntimeError(summary + " | " + "; ".join(errors))
    return summary


@functions_framework.http
def run(request):
    return execute()


if __name__ == "__main__":
    print(execute())
