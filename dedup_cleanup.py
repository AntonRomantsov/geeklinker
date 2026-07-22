from __future__ import annotations

from datetime import date, datetime
from urllib.parse import urlparse

from config import Config
from feeds import fetch_rss_items, fetch_scrape_items
from sheets import SheetsClient, SourceRow

DATE_FORMAT = "%d.%m.%Y"


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, DATE_FORMAT).date()
    except (ValueError, TypeError):
        return None


def cleanup_site(cfg: Config, sheets: SheetsClient, site_name: str, all_sources: list[SourceRow]) -> int:
    """Удаляет из _Dedup записи данного сайта, устаревшие относительно всех его текущих потоков.
    Возвращает число удалённых строк."""
    site_sources = [s for s in all_sources if s.site_name == site_name and s.source_type in ("rss", "scrape")]
    if not site_sources:
        return 0

    domains = {urlparse(source.url).netloc for source in site_sources}
    thresholds = []

    for source in site_sources:
        try:
            items = fetch_rss_items(source.url) if source.source_type == "rss" else fetch_scrape_items(source.url, cfg)
        except Exception:
            continue  # не удалось получить текущее состояние потока - пропускаем его в этот раз

        dates = [d for d in (_parse_date(item.pub_date) for item in items) if d is not None]
        if dates:
            thresholds.append(min(dates))

    if not thresholds:
        return 0
    cutoff = min(thresholds)

    dedup_ws = sheets._spreadsheet.worksheet(cfg.dedup_sheet_name)
    rows = dedup_ws.get_all_values()
    if not rows:
        return 0
    header, data_rows = rows[0], rows[1:]

    rows_to_keep = []
    removed = 0
    for row in data_rows:
        if len(row) < 2:
            rows_to_keep.append(row)
            continue
        url, date_str = row[0], row[1]
        row_date = _parse_date(date_str)
        if urlparse(url).netloc in domains and row_date is not None and row_date < cutoff:
            removed += 1
            continue
        rows_to_keep.append(row)

    if removed:
        dedup_ws.clear()
        dedup_ws.update("A1", [header] + rows_to_keep, value_input_option="USER_ENTERED")

    return removed


def cleanup_orphaned_domains(cfg: Config, sheets: SheetsClient, all_config_urls: list[str]) -> int:
    """Удаляет из _Dedup записи, чей домен не встречается вообще ни в одной строке blog_config
    (активной или нет) - источник был полностью удалён из конфига, сверяться не с чем,
    так что удаляем без проверки даты. Возвращает число удалённых строк."""
    known_domains = {urlparse(url).netloc for url in all_config_urls if url}

    dedup_ws = sheets._spreadsheet.worksheet(cfg.dedup_sheet_name)
    rows = dedup_ws.get_all_values()
    if not rows:
        return 0
    header, data_rows = rows[0], rows[1:]

    rows_to_keep = []
    removed = 0
    for row in data_rows:
        if not row or not row[0]:
            rows_to_keep.append(row)
            continue
        domain = urlparse(row[0]).netloc
        if domain and domain not in known_domains:
            removed += 1
            continue
        rows_to_keep.append(row)

    if removed:
        dedup_ws.clear()
        dedup_ws.update("A1", [header] + rows_to_keep, value_input_option="USER_ENTERED")

    return removed
