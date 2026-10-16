from __future__ import annotations

import sys

import functions_framework
import gspread
import requests
from google.genai import errors as genai_errors

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
    if isinstance(exc, genai_errors.APIError) and exc.code in TRANSIENT_HTTP_STATUS_CODES:
        return True
    if isinstance(exc, gspread.exceptions.APIError):
        # exc.code - это код из тела ответа (JSON), а не обязательно настоящий HTTP-статус:
        # если Google вместо JSON с ошибкой API отдал что-то другое (например, HTML-страницу
        # инфраструктурной 502 от Google Frontend), gspread не может его разобрать и
        # подставляет code=-1 как заглушку. Настоящий HTTP-статус при этом всё равно есть
        # в exc.response - проверяем его в приоритете.
        status_code = exc.response.status_code if exc.response is not None else exc.code
        if status_code in TRANSIENT_HTTP_STATUS_CODES:
            return True
    return False


def _error_details_mention(detail_blob: str, source_key: str) -> bool:
    return any(line.startswith(source_key) for line in detail_blob.split("\n"))


def execute(dry_run: bool = False) -> str:
    cfg = Config.from_env()

    run_started_at: str | None = None
    try:
        sheets = SheetsClient(cfg)
        if not dry_run:
            lock_result = sheets.acquire_run_lock()
            if lock_result is None:
                # Другой прогон ещё выполняется (моложе 5 минут) - молча выходим:
                # ничего не пишем, ничего не отправляем.
                return "Прогон уже выполняется - новый не запущен."
            run_started_at, hung_message = lock_result
            if hung_message:
                # Красные ошибки уведомляются всегда, независимо от требует/не требует
                # вмешательства.
                send_fatal_alert(cfg, hung_message)
        active_sources = sheets.load_active_sources()
        dedup_links = sheets.load_dedup_links()
    except Exception as exc:
        if dry_run:
            raise
        # Настройка не удалась - дальнейшая обработка в этом запуске невозможна.
        send_fatal_alert(cfg, str(exc))
        try:
            processed = "да" if _is_transient_error(exc) else "нет"
            SheetsClient(cfg).write_fatal_log_row(str(exc), processed)
        except Exception:
            pass  # если и лог записать не получилось - не мешаем исходному исключению всплыть
        raise

    report: list[str] = ["[DRY RUN] Ничего не записано в таблицы, уведомления не отправлены.\n"] if dry_run else []

    target_sheet_cache: dict[str, set[str]] = {}
    source_errors: list[tuple[str, bool]] = []  # (сообщение, транзиентная ли ошибка)
    other_errors: list[tuple[str, bool]] = []
    new_articles = 0
    sites_with_new_articles: set[str] = set()
    target_sheets_written: set[str] = set()

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
                    target_sheets_written.add(source.target_sheet)

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

    if source_errors:
        # Эскалация: даже транзиентная по типу ошибка перестаёт считаться "не требующей
        # вмешательства", если она же повторяется у этого источника три раза подряд -
        # значит сама по себе она не проходит.
        recent_details = sheets.get_recent_error_details(2)
        escalated_source_errors = []
        for message, transient in source_errors:
            source_key = message.split(":", 1)[0]
            if (
                transient
                and len(recent_details) == 2
                and all(_error_details_mention(detail, source_key) for detail in recent_details)
            ):
                transient = False
            escalated_source_errors.append((message, transient))
        source_errors = escalated_source_errors

    for sheet_name in target_sheets_written:
        try:
            sheets.sort_target_sheet_by_date(sheet_name)
        except Exception as exc:  # noqa: BLE001 - сортировка не должна ронять весь прогон
            other_errors.append((f"Сортировка листа «{sheet_name}»: {exc}", _is_transient_error(exc)))

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

    all_errors = source_errors + other_errors

    # В Telegram уходят все ошибки, которые требуют вмешательства (по критерию: сами не
    # исчезнут при следующем прогоне) - и по источникам, и по обслуживающим шагам вроде
    # сортировки/чистки _Dedup. Транзиентные (403/429/5xx, таймауты) - только в лог.
    telegram_worthy = [message for message, transient in all_errors if not transient]
    if telegram_worthy:
        try:
            send_error_alert(cfg, telegram_worthy)
        except Exception as exc:  # noqa: BLE001 - все данные уже записаны, просто фиксируем как warning
            other_errors.append((f"Не удалось отправить уведомление в Telegram: {exc}", _is_transient_error(exc)))
            all_errors = source_errors + other_errors  # пересчитать с учётом новой ошибки

    try:
        if all_errors:
            requires_intervention = any(not transient for _message, transient in all_errors)
            processed = "нет" if requires_intervention else "да"
            details = "\n".join(message for message, _transient in all_errors)
            sheets.finish_run_log(
                run_started_at, "error", new_articles, len(all_errors), dedup_removed, details, processed
            )
        else:
            sheets.finish_run_log(run_started_at, "success", new_articles, 0, dedup_removed, "", "да")
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
