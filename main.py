from __future__ import annotations

import functions_framework

from bot_webhook import telegram_webhook  # noqa: F401 - должна быть видна в main.py для functions-framework
from config import Config
from dedup_cleanup import cleanup_site
from feeds import fetch_rss_items, fetch_scrape_items
from notify import send_blocked_source_warning, send_error_alert, send_fatal_alert, send_new_article
from rss_discovery import discover_feed_url, is_allowed_by_robots
from sheets import SheetsClient

BLOCKED_TYPE = "!blocked"


def execute() -> str:
    cfg = Config.from_env()

    try:
        sheets = SheetsClient(cfg)
        active_sources = sheets.load_active_sources()
        dedup_links = sheets.load_dedup_links()
    except Exception as exc:
        # Настройка не удалась - дальнейшая обработка в этом запуске невозможна.
        send_fatal_alert(cfg, str(exc))
        try:
            SheetsClient(cfg).append_run_log("fatal", 0, 1, 0, str(exc))
        except Exception:
            pass  # если и лог записать не получилось - не мешаем исходному исключению всплыть
        raise

    target_sheet_cache: dict[str, set[str]] = {}
    source_errors: list[str] = []
    other_errors: list[str] = []
    new_articles = 0
    sites_with_new_articles: set[str] = set()

    for source in active_sources:
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
                sites_with_new_articles.add(source.site_name)

        except Exception as exc:  # noqa: BLE001 - источник не обработан, но остальные продолжаем
            source_errors.append(f"{source.site_name} ({source.url}): {exc}")

    dedup_removed = 0
    for site_name in sites_with_new_articles:
        try:
            dedup_removed += cleanup_site(cfg, sheets, site_name, active_sources)
        except Exception as exc:  # noqa: BLE001 - чистка не должна ронять весь прогон
            other_errors.append(f"Чистка _Dedup для «{site_name}»: {exc}")

    if source_errors:
        send_error_alert(cfg, source_errors)

    all_errors = source_errors + other_errors
    try:
        sheets.append_run_log(
            "error" if all_errors else "success",
            new_articles,
            len(all_errors),
            dedup_removed,
            "; ".join(all_errors),
        )
    except Exception:
        pass  # не смогли записать лог - не роняем из-за этого сам прогон

    summary = f"Готово. Новых статей: {new_articles}. Ошибок: {len(all_errors)}"
    if all_errors:
        raise RuntimeError(summary + " | " + "; ".join(all_errors))
    return summary


@functions_framework.http
def run(request):
    return execute()


if __name__ == "__main__":
    print(execute())
