from __future__ import annotations

import sys

import functions_framework
import requests

from bot_webhook import telegram_webhook  # noqa: F401 - должна быть видна в main.py для functions-framework
from config import Config
from dedup_cleanup import cleanup_orphaned_domains, cleanup_site
from feeds import fetch_rss_items, fetch_scrape_items
from notify import send_blocked_source_warning, send_error_alert, send_fatal_alert, send_new_article
from rss_discovery import discover_feed_url, is_allowed_by_robots
from sheets import SheetsClient

BLOCKED_TYPE = "!blocked"

# Ошибки такого рода не связаны с нашим кодом и не постоянны - в теории должны исчезнуть
# сами собой на следующем прогоне. О них не нужно уведомлять в Telegram, только в лог.
TRANSIENT_HTTP_STATUS_CODES = {403, 429, 500, 502, 503, 504}


def _is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        if response is not None and response.status_code in TRANSIENT_HTTP_STATUS_CODES:
            return True
    return False


def execute(dry_run: bool = False) -> str:
    cfg = Config.from_env()

    try:
        sheets = SheetsClient(cfg)
        active_sources = sheets.load_active_sources()
        dedup_links = sheets.load_dedup_links()
    except Exception as exc:
        if dry_run:
            raise
        # Настройка не удалась - дальнейшая обработка в этом запуске невозможна.
        send_fatal_alert(cfg, str(exc))
        try:
            SheetsClient(cfg).append_run_log("fatal", 0, 1, 0, str(exc))
        except Exception:
            pass  # если и лог записать не получилось - не мешаем исходному исключению всплыть
        raise

    report: list[str] = ["[DRY RUN] Ничего не записано в таблицы, уведомления не отправлены.\n"] if dry_run else []

    target_sheet_cache: dict[str, set[str]] = {}
    source_errors: list[tuple[str, bool]] = []  # (сообщение, транзиентная ли ошибка)
    other_errors: list[tuple[str, bool]] = []
    new_articles = 0
    sites_with_new_articles: set[str] = set()

    for source in active_sources:
        try:
            if source.source_type == BLOCKED_TYPE:
                if dry_run:
                    report.append(f"{source.site_name}: заблокирован (!blocked), пропущен")
                else:
                    send_blocked_source_warning(cfg, source.site_name, source.url)
                continue

            if source.source_type not in ("rss", "scrape"):
                feed_url = discover_feed_url(source.url)
                if feed_url and is_allowed_by_robots(feed_url):
                    source.url = feed_url
                    source.source_type = "rss"
                    if dry_run:
                        report.append(f"{source.site_name}: определился бы как rss ({feed_url})")
                    else:
                        sheets.update_source_type(source.row_number, source.url, source.source_type)
                elif is_allowed_by_robots(source.url):
                    source.source_type = "scrape"
                    if dry_run:
                        report.append(f"{source.site_name}: определился бы как scrape")
                    else:
                        sheets.update_source_type(source.row_number, source.url, source.source_type)
                else:
                    source.source_type = BLOCKED_TYPE
                    if dry_run:
                        report.append(f"{source.site_name}: стал бы !blocked (запрещён и rss, и scrape)")
                    else:
                        sheets.update_source_type(source.row_number, source.url, source.source_type)
                        send_blocked_source_warning(cfg, source.site_name, source.url)
                    continue

            target_links = target_sheet_cache.setdefault(
                source.target_sheet, sheets.load_sheet_links(source.target_sheet)
            )

            if source.source_type == "rss":
                try:
                    items = fetch_rss_items(source.url)
                except RuntimeError:
                    # Тип явно "rss", но по этому URL это не фид, а обычная страница -
                    # ищем реальный фид на ней.
                    feed_url = discover_feed_url(source.url)
                    if feed_url:
                        source.url = feed_url
                        if dry_run:
                            report.append(
                                f"{source.site_name}: в колонке B была страница вместо фида, "
                                f"урл заменён на найденный ({feed_url})"
                            )
                        else:
                            sheets.update_source_type(source.row_number, source.url, source.source_type)
                        items = fetch_rss_items(source.url)
                    else:
                        source.source_type = "scrape"
                        if dry_run:
                            report.append(f"{source.site_name}: фид на странице не найден, переключен на scrape")
                        else:
                            sheets.update_source_type(source.row_number, source.url, source.source_type)
                        items = fetch_scrape_items(source.url, cfg)
            else:
                items = fetch_scrape_items(source.url, cfg)

            source_new = 0
            for item in items:
                if item.link in dedup_links or item.link in target_links:
                    continue

                if dry_run:
                    report.append(
                        f"{source.site_name} -> {source.target_sheet}: "
                        f'новая статья "{item.title}" — {item.link} ({item.pub_date})'
                    )
                else:
                    sheets.append_dedup(item.link, item.pub_date)
                    sheets.append_article(
                        source.target_sheet, source.site_name, item.link, item.title, item.pub_date
                    )
                    send_new_article(cfg, source.site_name, source.target_sheet, item)

                dedup_links.add(item.link)
                target_links.add(item.link)
                new_articles += 1
                source_new += 1
                sites_with_new_articles.add(source.site_name)

            if dry_run and source_new == 0:
                report.append(f"{source.site_name}: новых статей нет")

        except Exception as exc:  # noqa: BLE001 - источник не обработан, но остальные продолжаем
            message = f"{source.site_name} ({source.url}): {exc}"
            if dry_run:
                report.append(f"ОШИБКА: {message}")
            else:
                source_errors.append((message, _is_transient_error(exc)))

    if dry_run:
        report.append(f"\nИтого: новых статей нашлось бы {new_articles}.")
        return "\n".join(report)

    dedup_removed = 0
    for site_name in sites_with_new_articles:
        try:
            dedup_removed += cleanup_site(cfg, sheets, site_name, active_sources)
        except Exception as exc:  # noqa: BLE001 - чистка не должна ронять весь прогон
            other_errors.append((f"Чистка _Dedup для «{site_name}»: {exc}", _is_transient_error(exc)))

    try:
        all_config_urls = sheets.load_all_source_urls()
        dedup_removed += cleanup_orphaned_domains(cfg, sheets, all_config_urls)
    except Exception as exc:  # noqa: BLE001 - чистка не должна ронять весь прогон
        other_errors.append((f"Чистка _Dedup от удалённых источников: {exc}", _is_transient_error(exc)))

    # В Telegram уходят только ошибки обработки источников, которые не транзиентные -
    # временные внешние сбои (403/429/5xx, таймауты) и ошибки чистки _Dedup туда не пишем.
    telegram_worthy = [message for message, transient in source_errors if not transient]
    if telegram_worthy:
        try:
            send_error_alert(cfg, telegram_worthy)
        except Exception as exc:  # noqa: BLE001 - все данные уже записаны, просто фиксируем как warning
            other_errors.append((f"Не удалось отправить уведомление в Telegram: {exc}", _is_transient_error(exc)))

    all_errors = source_errors + other_errors
    try:
        if all_errors:
            details = "\n".join(message for message, _transient in all_errors)
            sheets.append_run_log("error", new_articles, len(all_errors), dedup_removed, details)
        else:
            sheets.append_run_log("success", new_articles, 0, dedup_removed, "")
    except Exception:
        pass  # не смогли записать лог - не роняем из-за этого сам прогон

    error_messages = [message for message, _transient in all_errors]
    summary = f"Готово. Новых статей: {new_articles}. Ошибок: {len(all_errors)}"
    if all_errors:
        raise RuntimeError(summary + " | " + "; ".join(error_messages))
    return summary


@functions_framework.http
def run(request):
    dry_run = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    return execute(dry_run=dry_run)


if __name__ == "__main__":
    print(execute(dry_run="--dry-run" in sys.argv))
