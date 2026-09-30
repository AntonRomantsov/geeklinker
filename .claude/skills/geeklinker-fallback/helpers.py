"""
Вспомогательный CLI для скилла geeklinker-fallback (третий контур обработки GeekLinker).

Все команды запускаются из корня geeklinker_bot (где лежит sa-key.json/.env) и
переиспользуют существующие модули проекта: config.py, sheets.py, feeds.py,
notify.py, browser_headers.py, rss_discovery.py. Извлечение статей для scrape-
источников Gemini НЕ использует - это делает сам Claude, читая сохранённый HTML.

Команды:
  list_sources
      Печатает JSON-список ВСЕХ активных источников (любой engine, кроме
      !blocked) - и gc, и make. Этот скилл - подстраховка поверх обоих
      обработчиков, поэтому фильтр по engine не применяется.

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
"""

from __future__ import annotations

import json
import sys

# На Windows stdout по умолчанию не UTF-8 - без этого кириллица портится не только
# в отображении терминала, но и в байтах при перенаправлении в файл.
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")

from config import Config  # noqa: E402
from feeds import Article, _get_with_retry, fetch_rss_items  # noqa: E402
from notify import send_new_article  # noqa: E402
from rss_discovery import is_allowed_by_robots  # noqa: E402
from sheets import SheetsClient  # noqa: E402

BLOCKED_TYPE = "!blocked"


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
    else:
        print(f"Неизвестная команда: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
