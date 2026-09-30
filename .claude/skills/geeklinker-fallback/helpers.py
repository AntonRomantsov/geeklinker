"""
Вспомогательный CLI для скилла geeklinker-fallback (третий контур обработки GeekLinker).

Все команды запускаются из корня geeklinker_bot (где лежит sa-key.json/.env) и
переиспользуют существующие модули проекта: config.py, sheets.py, feeds.py,
notify.py, browser_headers.py, rss_discovery.py. Извлечение статей для scrape-
источников Gemini НЕ использует - это делает сам Claude, читая сохранённый HTML.

Команды:
  list_sources
      Печатает JSON-список активных источников с engine == "claude" (и
      только их - другие источники, обслуживаемые gc/make, этот скилл не
      трогает).

  fetch_rss <url>
      Парсит RSS напрямую (feedparser, без Gemini) и печатает JSON-список
      статей [{"link":..., "title":..., "pub_date":...}, ...]. При ошибке -
      {"error": "...", "transient": true/false} (см. критерий транзиентности
      ниже - тот же, что и в main.py).

  fetch_scrape_html <url> <output_file>
      Скачивает HTML страницы (с теми же заголовками/cookie, что и сам
      скрипт) и сохраняет в output_file. Дальше HTML нужно прочитать и
      извлечь статьи самостоятельно (см. SKILL.md). При ошибке - тот же
      формат {"error", "transient"}, что и у fetch_rss.

  check_dedup <urls_json_file>
      На входе - JSON-файл со списком url. Печатает JSON-список тех url,
      которых ещё нет в _Dedup (то есть реально новых).

  add_article <site_name> <target_sheet> <url> <title> <pub_date_ddmmyyyy>
      Добавляет статью в _Dedup и в целевой лист (тем же способом, что и
      основной скрипт - формула HYPERLINK), сортирует целевой лист по дате,
      отправляет уведомление в Telegram.

  finish_run <new_articles> <errors_json_file>
      Единая команда для завершения прогона - вызывается ОДИН раз в самом
      конце (см. SKILL.md, шаг 5). errors_json_file - JSON-список всех
      ошибок, встреченных за прогон, в формате
      [{"source_key": "Название (url)", "message": "полный текст ошибки",
        "transient": true/false}, ...] (transient - то, что вернула команда,
      которая эту ошибку породила; если ошибку получил сам Claude при
      разборе HTML - используй false, если непонятно, самостоятельно
      определить транзиентность нельзя).

      Делает всё сразу, по тому же принципу, что и main.py:
        1. Эскалация: если ошибка транзиентна, но тот же source_key уже
           встречался в ДВУХ предыдущих прогонах ЭТОГО скилла подряд - она
           перестаёт считаться транзиентной (требует вмешательства), даже
           если по типу не требовала.
        2. Пишет одну строку в лист "Логи" (как раньше - одним цельным
           действием, без двухфазной вставки): "Обработано"="да", если ПОСЛЕ
           эскалации все ошибки остались транзиентными (или ошибок не было
           вовсе), иначе "нет". В "Детали ошибок" - все ошибки текстом,
           начиная с пометки "[Claude AI Skill]".
        3. В Telegram отправляется ОТДЕЛЬНЫМ от основного скрипта сообщением
           (свой заголовок, через notify.send_error_alert/GeekLinkerBot)
           только то, что после эскалации осталось нетранзиентным. Если
           таких нет - в Telegram ничего не шлётся вовсе (даже при наличии
           чисто транзиентных ошибок - они только в логе).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# На Windows stdout по умолчанию не UTF-8 - без этого кириллица портится не только
# в отображении терминала, но и в байтах при перенаправлении в файл.
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

import gspread  # noqa: E402
import requests  # noqa: E402
from google.genai import errors as genai_errors  # noqa: E402

from config import Config  # noqa: E402
from feeds import Article, _get_with_retry, fetch_rss_items  # noqa: E402
from notify import _send, send_new_article  # noqa: E402
from rss_discovery import is_allowed_by_robots  # noqa: E402
from sheets import LOG_STATUS_LABELS, LOG_TIMESTAMP_FORMAT, SheetsClient  # noqa: E402

BLOCKED_TYPE = "!blocked"
ENGINE_FOR_THIS_SKILL = "claude"
SKILL_LOG_TAG = "[Claude AI Skill]"

# Тот же критерий, что и в main.py::_is_transient_error - ошибки такого рода не
# связаны с нашим кодом и не постоянны, в теории должны исчезнуть сами собой на
# следующем прогоне. Продублировано здесь (а не импортировано из main.py), чтобы
# не тянуть за собой весь граф импортов основного скрипта (bot_webhook и т.п.)
# ради одной маленькой функции.
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
    if isinstance(exc, gspread.exceptions.APIError) and exc.code in TRANSIENT_HTTP_STATUS_CODES:
        return True
    return False


def _error_details_mention(detail_blob: str, source_key: str) -> bool:
    return any(line.startswith(source_key) for line in detail_blob.split("\n"))


def cmd_list_sources() -> None:
    cfg = Config.from_env()
    client = SheetsClient(cfg)
    rows = client._spreadsheet.worksheet(cfg.config_sheet_name).get_all_values()[1:]
    sources = []
    for row in rows:
        if len(row) < 6:
            continue
        site_name, url, source_type, target_sheet, engine, status = row[:6]
        if status.strip().lower() != "активен" or not url.strip():
            continue
        if engine.strip().lower() != ENGINE_FOR_THIS_SKILL:
            continue
        source_type = source_type.strip().lower()
        if source_type == BLOCKED_TYPE:
            continue
        sources.append(
            {
                "site_name": site_name.strip(),
                "url": url.strip(),
                "source_type": source_type,
                "target_sheet": target_sheet.strip(),
                "engine": engine.strip(),
            }
        )
    print(json.dumps(sources, ensure_ascii=False, indent=2))


def cmd_fetch_rss(url: str) -> None:
    if not is_allowed_by_robots(url):
        print(json.dumps({"error": f"robots.txt запрещает доступ к {url}", "transient": False}, ensure_ascii=False))
        return
    try:
        items = fetch_rss_items(url)
    except Exception as exc:  # noqa: BLE001 - хотим сообщить о любой ошибке, не падать
        print(json.dumps({"error": str(exc), "transient": _is_transient_error(exc)}, ensure_ascii=False))
        return
    print(json.dumps([{"link": i.link, "title": i.title, "pub_date": i.pub_date} for i in items], ensure_ascii=False))


def cmd_fetch_scrape_html(url: str, output_file: str) -> None:
    if not is_allowed_by_robots(url):
        print(json.dumps({"error": f"robots.txt запрещает доступ к {url}", "transient": False}, ensure_ascii=False))
        return
    cfg = Config.from_env()
    try:
        response = _get_with_retry(url, headers={"Cookie": cfg.scrape_cookie_header})
    except Exception as exc:  # noqa: BLE001 - хотим сообщить о любой ошибке, не падать
        print(json.dumps({"error": str(exc), "transient": _is_transient_error(exc)}, ensure_ascii=False))
        return
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(response.text)
    print(json.dumps({"saved_to": output_file, "bytes": len(response.text)}, ensure_ascii=False))


def cmd_check_dedup(urls_json_file: str) -> None:
    cfg = Config.from_env()
    client = SheetsClient(cfg)
    dedup = client.load_dedup_links()
    urls = json.load(open(urls_json_file, encoding="utf-8"))
    new_urls = [u for u in urls if u not in dedup]
    print(json.dumps(new_urls, ensure_ascii=False))


def cmd_add_article(site_name: str, target_sheet: str, url: str, title: str, pub_date: str) -> None:
    cfg = Config.from_env()
    client = SheetsClient(cfg)
    dedup = client.load_dedup_links()
    if url in dedup:
        print(json.dumps({"skipped": "уже есть в _Dedup", "url": url}, ensure_ascii=False))
        return
    client.append_dedup(url, pub_date)
    client.append_article(target_sheet, site_name, url, title, pub_date)
    client.sort_target_sheet_by_date(target_sheet)
    send_new_article(cfg, site_name, target_sheet, Article(link=url, title=title, pub_date=pub_date))
    print(json.dumps({"added": url, "target_sheet": target_sheet}, ensure_ascii=False))


def _own_recent_error_details(client: SheetsClient, count: int, scan_limit: int = 200) -> list[str]:
    """Последние count строк листа "Логи", написанных ЭТИМ скиллом (по тегу
    SKILL_LOG_TAG), в порядке от самой свежей. Строки основного скрипта
    (между прогонами скилла их может быть много - скилл раз в сутки, скрипт
    чаще) пропускаются - иначе "последние 2 строки" были бы почти всегда
    чужими."""
    worksheet = client._get_or_create_log_sheet()
    rows = worksheet.get_all_values()[1 : 1 + scan_limit]
    own: list[str] = []
    for row in rows:
        details = row[5] if len(row) > 5 else ""
        if details.startswith(SKILL_LOG_TAG):
            own.append(details)
            if len(own) == count:
                break
    return own


def cmd_finish_run(new_articles: str, errors_json_file: str) -> None:
    cfg = Config.from_env()
    client = SheetsClient(cfg)

    new_articles_n = int(new_articles)
    errors = json.load(open(errors_json_file, encoding="utf-8"))

    # 1. Эскалация: транзиентная ошибка, повторившаяся у того же источника два
    #    прогона подряд (плюс этот - итого три раза), перестаёт считаться
    #    транзиентной - тот же принцип, что в main.py.
    recent_details = _own_recent_error_details(client, 2) if errors else []
    for err in errors:
        if err.get("transient") and len(recent_details) == 2:
            if all(_error_details_mention(detail, err["source_key"]) for detail in recent_details):
                err["transient"] = False

    # 2. Лог - одна строка, одним цельным действием (см. write_log раньше).
    requires_intervention = any(not e.get("transient") for e in errors)
    if errors:
        status = "error"  # -> label "warning", оранжевая строка
        processed = "нет" if requires_intervention else "да"
        details = SKILL_LOG_TAG + "\n" + "\n".join(e["message"] for e in errors)
    else:
        status = "success"
        processed = "да"
        details = SKILL_LOG_TAG

    worksheet = client._get_or_create_log_sheet()
    timestamp = datetime.now(ZoneInfo("Europe/Kyiv")).strftime(LOG_TIMESTAMP_FORMAT)
    label = LOG_STATUS_LABELS[status]
    # RAW, а не USER_ENTERED - иначе Sheets распознаёт метку времени как настоящую
    # дату и переформатирует при чтении (та же причина, что и в sheets.py).
    worksheet.insert_row(
        [timestamp, label, new_articles_n, len(errors), 0, details, processed],
        index=2,
        value_input_option="RAW",
    )
    client._format_log_row(worksheet, 2, status)
    client._prune_log_sheet(worksheet)

    # 3. Telegram - отдельным от основного скрипта сообщением, только то, что
    #    после эскалации требует вмешательства. Чисто транзиентные ошибки -
    #    только в логе, как и у основного скрипта.
    telegram_worthy = [e["message"] for e in errors if not e.get("transient")]
    if telegram_worthy:
        text = "⚠️ <b>GeekLinker: ошибки в AI-контуре (geeklinker-fallback)</b>\n\n" + "\n".join(
            f"• {m}" for m in telegram_worthy
        )
        _send(cfg, cfg.telegram_alert_chat_id, text)

    print(
        json.dumps(
            {
                "status": status,
                "processed": processed,
                "errors_total": len(errors),
                "errors_requiring_intervention": len(telegram_worthy),
                "notified_telegram": bool(telegram_worthy),
            },
            ensure_ascii=False,
        )
    )


def cmd_notify_errors(errors_json_file: str) -> None:
    """Оставлено как самостоятельный примитив (на случай, если понадобится
    отправить что-то в Telegram отдельно от finish_run) - сам finish_run эту
    функцию не использует, у него своя логика с учётом эскалации."""
    cfg = Config.from_env()
    errors = json.load(open(errors_json_file, encoding="utf-8"))
    if not errors:
        print(json.dumps({"skipped": "пустой список ошибок"}, ensure_ascii=False))
        return
    text = "⚠️ <b>GeekLinker: ошибки в AI-контуре (geeklinker-fallback)</b>\n\n" + "\n".join(
        f"• {error}" for error in errors
    )
    _send(cfg, cfg.telegram_alert_chat_id, text)
    print(json.dumps({"sent": len(errors)}, ensure_ascii=False))


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]

    if command == "list_sources":
        cmd_list_sources()
    elif command == "fetch_rss":
        cmd_fetch_rss(*args)
    elif command == "fetch_scrape_html":
        cmd_fetch_scrape_html(*args)
    elif command == "check_dedup":
        cmd_check_dedup(*args)
    elif command == "add_article":
        cmd_add_article(*args)
    elif command == "finish_run":
        cmd_finish_run(*args)
    elif command == "notify_errors":
        cmd_notify_errors(*args)
    else:
        print(f"Неизвестная команда: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
