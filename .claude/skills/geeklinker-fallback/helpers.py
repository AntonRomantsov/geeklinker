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
      статей [{"link":..., "title":..., "pub_date":...}, ...].

  fetch_scrape_html <url> <output_file>
      Скачивает HTML страницы (с теми же заголовками/cookie, что и сам
      скрипт) и сохраняет в output_file. Дальше HTML нужно прочитать и
      извлечь статьи самостоятельно (см. SKILL.md).

  check_dedup <urls_json_file>
      На входе - JSON-файл со списком url. Печатает JSON-список тех url,
      которых ещё нет в _Dedup (то есть реально новых).

  add_article <site_name> <target_sheet> <url> <title> <pub_date_ddmmyyyy>
      Добавляет статью в _Dedup и в целевой лист (тем же способом, что и
      основной скрипт - формула HYPERLINK), сортирует целевой лист по дате,
      отправляет уведомление в Telegram.

  notify_errors <errors_json_file>
      На входе - JSON-файл со списком строк-описаний ошибок, встреченных
      за время работы скилла. Отправляет их одним сообщением в Telegram
      через GeekLinkerBot (тот же бот, что и у основного скрипта).

  write_log <new_articles> <errors_count> <error_details_or_empty>
      Пишет одну строку в лист "Логи" по тому же принципу, что и основной
      скрипт (новая строка наверху, цвет по статусу), но ОДНИМ цельным
      действием - не двухфазной вставкой, как делает скрипт. Так безопаснее:
      двухфазная вставка (метка времени сначала, дозаполнение потом) - это
      часть механизма блокировки параллельных прогонов скрипта, и если бы
      скилл её тоже использовал, он мог бы случайно заблокировать реальный
      прогон скрипта или спрятать от него зависшую строку. Поле "Детали
      ошибок" всегда начинается с "[Claude AI Skill]" - с самим текстом
      ошибок при errors_count > 0, либо одной этой пометкой при успешном
      прогоне (errors_count == 0).
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

from config import Config  # noqa: E402
from feeds import Article, _get_with_retry, fetch_rss_items  # noqa: E402
from notify import _send, send_new_article  # noqa: E402
from rss_discovery import is_allowed_by_robots  # noqa: E402
from sheets import LOG_STATUS_LABELS, LOG_TIMESTAMP_FORMAT, SheetsClient  # noqa: E402

BLOCKED_TYPE = "!blocked"
ENGINE_FOR_THIS_SKILL = "claude"


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
        print(json.dumps({"error": f"robots.txt запрещает доступ к {url}"}, ensure_ascii=False))
        return
    items = fetch_rss_items(url)
    print(json.dumps([{"link": i.link, "title": i.title, "pub_date": i.pub_date} for i in items], ensure_ascii=False))


def cmd_fetch_scrape_html(url: str, output_file: str) -> None:
    if not is_allowed_by_robots(url):
        print(json.dumps({"error": f"robots.txt запрещает доступ к {url}"}, ensure_ascii=False))
        return
    cfg = Config.from_env()
    response = _get_with_retry(url, headers={"Cookie": cfg.scrape_cookie_header})
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


def cmd_notify_errors(errors_json_file: str) -> None:
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


def cmd_write_log(new_articles: str, errors_count: str, error_details: str) -> None:
    cfg = Config.from_env()
    client = SheetsClient(cfg)

    new_articles_n = int(new_articles)
    errors_count_n = int(errors_count)

    if errors_count_n > 0:
        status = "error"  # -> label "warning", оранжевая строка
        processed = "нет"
        details = "[Claude AI Skill]\n" + error_details if error_details else "[Claude AI Skill]"
    else:
        status = "success"
        processed = "да"
        details = "[Claude AI Skill]"

    worksheet = client._get_or_create_log_sheet()
    timestamp = datetime.now(ZoneInfo("Europe/Kyiv")).strftime(LOG_TIMESTAMP_FORMAT)
    label = LOG_STATUS_LABELS[status]
    # RAW, а не USER_ENTERED - иначе Sheets распознаёт метку времени как настоящую
    # дату и переформатирует при чтении (та же причина, что и в sheets.py).
    worksheet.insert_row(
        [timestamp, label, new_articles_n, errors_count_n, 0, details, processed],
        index=2,
        value_input_option="RAW",
    )
    client._format_log_row(worksheet, 2, status)
    client._prune_log_sheet(worksheet)
    print(json.dumps({"logged": True, "status": status}, ensure_ascii=False))


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
    elif command == "notify_errors":
        cmd_notify_errors(*args)
    elif command == "write_log":
        cmd_write_log(*args)
    else:
        print(f"Неизвестная команда: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
