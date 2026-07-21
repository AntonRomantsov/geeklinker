from __future__ import annotations

import re
from dataclasses import dataclass

import gspread
from google.auth import default as google_auth_default

from config import Config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HYPERLINK_RE = re.compile(r'^=HYPERLINK\("([^"]*)"\s*[,;]\s*"(?:[^"]|"")*"\)$', re.IGNORECASE)

TARGET_SHEET_HEADERS = ["Источник", "Ссылка ", "Дата"]
DEFAULT_COLUMN_WIDTH_PX = 100
LINK_COLUMN_WIDTH_PX = DEFAULT_COLUMN_WIDTH_PX * 3


@dataclass
class SourceRow:
    site_name: str
    url: str
    source_type: str
    target_sheet: str


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
        for row in rows:
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
                    )
                )
        return sources

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
                                    "startIndex": 1,
                                    "endIndex": 2,
                                },
                                "properties": {"pixelSize": LINK_COLUMN_WIDTH_PX},
                                "fields": "pixelSize",
                            }
                        }
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
