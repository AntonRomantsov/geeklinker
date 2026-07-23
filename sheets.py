from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from google.auth import default as google_auth_default

from config import Config
from dates import DATE_FORMAT

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HYPERLINK_RE = re.compile(r'^=HYPERLINK\("([^"]*)"\s*[,;]\s*"(?:[^"]|"")*"\)$', re.IGNORECASE)

TARGET_SHEET_HEADERS = ["Источник", "Ссылка ", "Дата"]
TARGET_SHEET_COLUMN_WIDTHS_PX = [200, 500, 100]

LOG_SHEET_NAME = "Логи"
LOG_SHEET_HEADERS = [
    "Дата и время",
    "Статус",
    "Новых статей",
    "Ошибок",
    "Удалено из _Dedup",
    "Детали ошибок",
    "Обработано",
]
LOG_SHEET_COLUMN_WIDTHS_PX = [140, 160, 90, 70, 110, 500, 100]
LOG_RETENTION_ROWS = 30
LOG_KEEP_VALUES = {"нет", "0", ""}

LOG_STATUS_LABELS = {
    "success": "success",
    "error": "warning",
    "fatal": "error",
}
LOG_STATUS_COLORS = {
    "success": {"red": 0.85, "green": 0.94, "blue": 0.85},
    "error": {"red": 1.0, "green": 0.9, "blue": 0.7},
    "fatal": {"red": 0.96, "green": 0.78, "blue": 0.78},
}


@dataclass
class SourceRow:
    site_name: str
    url: str
    source_type: str
    target_sheet: str
    row_number: int


FORMULA_LOCALES_WITH_SEMICOLON_SEPARATOR = {"ru", "uk", "de", "fr", "es", "it", "pl", "pt", "tr", "nl"}


class SheetsClient:
    def __init__(self, cfg: Config):
        credentials, _ = google_auth_default(scopes=SCOPES)
        self._client = gspread.authorize(credentials)
        self._spreadsheet = self._client.open_by_key(cfg.spreadsheet_id)
        self._cfg = cfg
        locale_lang = (self._spreadsheet.locale or "en").split("_")[0].lower()
        self._formula_separator = ";" if locale_lang in FORMULA_LOCALES_WITH_SEMICOLON_SEPARATOR else ","

    def load_active_sources(self) -> list[SourceRow]:
        rows = self._spreadsheet.worksheet(self._cfg.config_sheet_name).get_all_values()[1:]
        sources = []
        for i, row in enumerate(rows, start=2):
            if len(row) < 6:
                continue
            site_name, url, source_type, target_sheet, _engine, status = row[:6]
            if status.strip().lower() == "активен" and url.strip():
                sources.append(
                    SourceRow(
                        site_name=site_name.strip(),
                        url=url.strip(),
                        source_type=source_type.strip().lower(),
                        target_sheet=target_sheet.strip(),
                        row_number=i,
                    )
                )
        return sources

    def load_all_source_urls(self) -> list[str]:
        """Все URL из blog_config, независимо от статуса активен/неактивен - используется,
        чтобы понять, какие домены всё ещё "известны" конфигу (для чистки _Dedup от
        полностью удалённых источников)."""
        rows = self._spreadsheet.worksheet(self._cfg.config_sheet_name).get_all_values()[1:]
        return [row[1].strip() for row in rows if len(row) > 1 and row[1].strip()]

    def update_source_type(self, row_number: int, url: str, source_type: str) -> None:
        worksheet = self._spreadsheet.worksheet(self._cfg.config_sheet_name)
        worksheet.update(f"B{row_number}:C{row_number}", [[url, source_type]], value_input_option="USER_ENTERED")

    def load_dedup_links(self) -> set[str]:
        rows = self._spreadsheet.worksheet(self._cfg.dedup_sheet_name).get_all_values()[1:]
        return {row[0] for row in rows if row and row[0]}

    def load_sheet_links(self, sheet_name: str) -> set[str]:
        try:
            worksheet = self._spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            return set()
        # FORMULA, чтобы достать исходный url из гиперссылки в колонке B,
        # а не отображаемый текст (title).
        rows = worksheet.get_all_values(value_render_option="FORMULA")[1:]
        return {_extract_link(row[1]) for row in rows if len(row) > 1 and row[1]}

    def append_dedup(self, link: str, pub_date: str) -> None:
        self._spreadsheet.worksheet(self._cfg.dedup_sheet_name).append_row(
            [link, pub_date], value_input_option="USER_ENTERED"
        )

    def append_article(self, sheet_name: str, site_name: str, link: str, title: str, pub_date: str) -> None:
        worksheet = self._get_or_create_target_sheet(sheet_name)
        formula = _hyperlink_formula(link, title, self._formula_separator)
        worksheet.append_row([site_name, formula, pub_date], value_input_option="USER_ENTERED")

    def sort_target_sheet_by_date(self, sheet_name: str) -> None:
        """Пересортировывает лист по дате (колонка C) от самой ранней к самой поздней."""
        try:
            worksheet = self._spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            return

        rows = worksheet.get_all_values(value_render_option="FORMULA")
        if len(rows) <= 2:
            return  # заголовок и максимум одна запись - сортировать нечего

        header, data_rows = rows[0], rows[1:]
        data_rows.sort(key=lambda row: _parse_sheet_date(row[2] if len(row) > 2 else ""))

        worksheet.update("A1", [header] + data_rows, value_input_option="USER_ENTERED")

    def _get_or_create_target_sheet(self, sheet_name: str) -> gspread.Worksheet:
        try:
            return self._spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = self._spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=len(TARGET_SHEET_HEADERS))
            worksheet.append_row(TARGET_SHEET_HEADERS, value_input_option="USER_ENTERED")
            self._spreadsheet.batch_update(
                {
                    "requests": [
                        {
                            "updateDimensionProperties": {
                                "range": {
                                    "sheetId": worksheet.id,
                                    "dimension": "COLUMNS",
                                    "startIndex": i,
                                    "endIndex": i + 1,
                                },
                                "properties": {"pixelSize": width},
                                "fields": "pixelSize",
                            }
                        }
                        for i, width in enumerate(TARGET_SHEET_COLUMN_WIDTHS_PX)
                    ]
                }
            )
            return worksheet

    def append_run_log(
        self,
        status: str,
        new_articles: int,
        errors_count: int,
        dedup_removed: int,
        error_details: str,
    ) -> None:
        worksheet = self._get_or_create_log_sheet()
        timestamp = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%Y %H:%M:%S")
        label = LOG_STATUS_LABELS[status]
        processed = "да" if status == "success" else "нет"

        # Новые записи вставляются сразу после заголовка - остальные сдвигаются вниз.
        worksheet.insert_row(
            [timestamp, label, new_articles, errors_count, dedup_removed, error_details, processed],
            index=2,
            value_input_option="USER_ENTERED",
        )
        self._spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": worksheet.id,
                                "startRowIndex": 1,
                                "endRowIndex": 2,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(LOG_SHEET_HEADERS),
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": LOG_STATUS_COLORS[status],
                                    "wrapStrategy": "WRAP",
                                }
                            },
                            "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.wrapStrategy",
                        }
                    },
                    {
                        "autoResizeDimensions": {
                            "dimensions": {
                                "sheetId": worksheet.id,
                                "dimension": "ROWS",
                                "startIndex": 1,
                                "endIndex": 2,
                            }
                        }
                    },
                ]
            }
        )

        self._prune_log_sheet(worksheet)

    def _prune_log_sheet(self, worksheet: gspread.Worksheet) -> None:
        rows = worksheet.get_all_values()
        if len(rows) <= LOG_RETENTION_ROWS + 1:
            return  # заголовок + до 30 записей - трогать нечего

        rows_to_delete = []
        for sheet_row_number in range(LOG_RETENTION_ROWS + 2, len(rows) + 1):
            row = rows[sheet_row_number - 1]
            processed_value = row[6].strip().lower() if len(row) > 6 else ""
            if processed_value not in LOG_KEEP_VALUES:
                rows_to_delete.append(sheet_row_number)

        for sheet_row_number in sorted(rows_to_delete, reverse=True):
            worksheet.delete_rows(sheet_row_number)

    def _get_or_create_log_sheet(self) -> gspread.Worksheet:
        try:
            return self._spreadsheet.worksheet(LOG_SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = self._spreadsheet.add_worksheet(title=LOG_SHEET_NAME, rows=1000, cols=len(LOG_SHEET_HEADERS))
            worksheet.append_row(LOG_SHEET_HEADERS, value_input_option="USER_ENTERED")
            self._spreadsheet.batch_update(
                {
                    "requests": [
                        {
                            "updateDimensionProperties": {
                                "range": {
                                    "sheetId": worksheet.id,
                                    "dimension": "COLUMNS",
                                    "startIndex": i,
                                    "endIndex": i + 1,
                                },
                                "properties": {"pixelSize": width},
                                "fields": "pixelSize",
                            }
                        }
                        for i, width in enumerate(LOG_SHEET_COLUMN_WIDTHS_PX)
                    ]
                }
            )
            return worksheet


def _hyperlink_formula(url: str, title: str, separator: str) -> str:
    escaped_url = url.replace('"', '""')
    escaped_title = (title or url).replace('"', '""')
    return f'=HYPERLINK("{escaped_url}"{separator} "{escaped_title}")'


def _extract_link(cell_value: str) -> str:
    match = HYPERLINK_RE.match(cell_value.strip())
    return match.group(1) if match else cell_value.strip()


SHEETS_DATE_EPOCH = date(1899, 12, 30)


def _parse_sheet_date(value: str | int | float) -> date:
    # Некоторые строки (например, из разовых скриптов миграции/бэкфилла) хранят дату не
    # текстом "ДД.ММ.ГГГГ", а как настоящее Google Sheets значение типа "Дата" - тогда
    # value_render_option="FORMULA" отдаёт его как число (дней с 30.12.1899).
    if isinstance(value, (int, float)):
        try:
            return SHEETS_DATE_EPOCH + timedelta(days=value)
        except OverflowError:
            return date.min
    try:
        return datetime.strptime(value.strip(), DATE_FORMAT).date()
    except (ValueError, TypeError):
        return date.min  # нераспознанная дата - в начало списка, а не в конец
