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
LOG_TIMESTAMP_FORMAT = "%d.%m.%Y %H:%M:%S"
LOG_LOCK_WINDOW_MINUTES = 5

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
LOG_IN_PROGRESS_COLOR = {"red": 1.0, "green": 1.0, "blue": 1.0}


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
            site_name, url, source_type, target_sheet, engine, status = row[:6]
            # engine == "gc" - источник обрабатывается этим скриптом (Google Cloud). Если
            # источник аварийно переведён на Make (engine != "gc"), скрипт его не трогает.
            if status.strip().lower() == "активен" and engine.strip().lower() == "gc" and url.strip():
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

    def get_recent_error_details(self, count: int) -> list[str]:
        """Возвращает содержимое колонки "Детали ошибок" последних count ЗАВЕРШЁННЫХ
        прогонов (строки начиная с 3-й - 2-я строка в момент вызова обычно ещё "наша"
        текущая незавершённая). Используется, чтобы определить, повторяется ли ошибка
        одного и того же источника подряд из прогона в прогон."""
        worksheet = self._get_or_create_log_sheet()
        rows = worksheet.get_all_values()
        data_rows = rows[2 : 2 + count]  # пропускаем заголовок (0) и текущую строку 2 (1)
        return [row[5] if len(row) > 5 else "" for row in data_rows]

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
        """Пересортировывает лист по дате (колонка C) от самой ранней к самой поздней.

        Строки переставляются нативной сортировкой Google Sheets (sortRange), а не
        перезаписью всего листа - это не трогает содержимое ячеек A/B вообще. Это важно:
        старая реализация читала весь лист и полностью перезаписывала его, из-за чего
        у вручную вставленных гиперссылок (это "богатое" форматирование текста, а не
        формула HYPERLINK) слетало форматирование - Values API его не видит и теряет
        при перезаписи. Формулы HYPERLINK, которые пишет сам скрипт, от этого бага не
        страдали (они читаются и пишутся как текст формулы), а вот ручные ссылки - да.

        Единственное, что дозаписывается - колонка C (дата), и только в тех строках, где
        она сейчас хранится текстом: нативная сортировка упорядочивает по фактическому
        типу ячейки, а текстовые даты "ДД.ММ.ГГГГ" сортировались бы лексикографически
        (неверно), а не хронологически. Колонки A/B при этом не трогаются."""
        try:
            worksheet = self._spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            return

        rows = worksheet.get_all_values(value_render_option="FORMULA")
        if len(rows) <= 2:
            return  # заголовок и максимум одна запись - сортировать нечего

        date_updates = []
        for i, row in enumerate(rows[1:], start=2):
            raw_date = row[2] if len(row) > 2 else ""
            if not isinstance(raw_date, str) or not raw_date.strip():
                continue  # уже настоящая дата (число) или пусто - трогать не нужно
            parsed = _parse_sheet_date(raw_date)
            if parsed != date.min:
                date_updates.append({"range": f"C{i}", "values": [[parsed.strftime(DATE_FORMAT)]]})

        if date_updates:
            worksheet.batch_update(date_updates, value_input_option="USER_ENTERED")

        worksheet.sort((3, "asc"), range=f"A2:C{len(rows)}")

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

    def acquire_run_lock(self) -> tuple[str, str | None] | None:
        """Проверяет верхнюю строку листа "Логи" перед стартом прогона.

        - Если там недозаполненная строка (только метка времени, без статуса) моложе
          LOG_LOCK_WINDOW_MINUTES - значит другой прогон ещё выполняется: молча
          возвращает None, ничего не пишет и не отправляет.
        - Если такая недозаполненная строка старше окна - считается зависшей
          (прогон, её создавший, не завершился штатно): дозаполняется как красная
          (fatal), после чего прогон продолжается как обычно.
        - Иначе (верхняя строка дозаполнена или её нет вовсе) - блокировки нет.

        Если блокировки нет, вставляет свою строку с меткой времени и возвращает
        (метка_времени, сообщение_о_зависшем_прогоне_или_None) - метку нужно передать
        в finish_run_log() в конце, а сообщение (если есть) - отправить в Telegram
        (красные ошибки уведомляются всегда, независимо от требует/не требует вмешательства).
        """
        worksheet = self._get_or_create_log_sheet()
        row = worksheet.row_values(2)
        timestamp_str = row[0] if row else ""
        status_cell = row[1] if len(row) > 1 else ""
        hung_message: str | None = None

        if timestamp_str and not status_cell:
            try:
                started_at = datetime.strptime(timestamp_str, LOG_TIMESTAMP_FORMAT)
            except ValueError:
                started_at = None
            if started_at is not None:
                elapsed = datetime.now(ZoneInfo("Europe/Kyiv")).replace(tzinfo=None) - started_at
                if elapsed < timedelta(minutes=LOG_LOCK_WINDOW_MINUTES):
                    return None  # другой прогон ещё выполняется - молча выходим
                hung_message = (
                    f"Прогон, начатый {timestamp_str}, не завершился штатно "
                    f"(обнаружено следующим запуском спустя {LOG_LOCK_WINDOW_MINUTES}+ минут)"
                )
                self._fill_hung_row(worksheet)

        timestamp = datetime.now(ZoneInfo("Europe/Kyiv")).strftime(LOG_TIMESTAMP_FORMAT)
        # RAW, а не USER_ENTERED - иначе Sheets распознаёт текст как настоящую дату/время
        # и при чтении обратно переформатирует её по-своему (убирая ведущий ноль у часа),
        # из-за чего finish_run_log() перестаёт находить свою строку по точному совпадению.
        worksheet.insert_row([timestamp], index=2, value_input_option="RAW")
        # Insert наследует форматирование соседней строки (например, зелёный от предыдущего
        # успешного прогона) - явно сбрасываем на белый, пока прогон не завершён.
        self._set_row_background(worksheet, 2, LOG_IN_PROGRESS_COLOR)
        return timestamp, hung_message

    def _fill_hung_row(self, worksheet: gspread.Worksheet) -> None:
        worksheet.update(
            "B2:G2",
            [
                [
                    LOG_STATUS_LABELS["fatal"],
                    0,
                    1,
                    0,
                    "Прогон не завершился штатно (обнаружен зависшим следующим запуском)",
                    "нет",
                ]
            ],
            value_input_option="USER_ENTERED",
        )
        self._format_log_row(worksheet, 2, "fatal")

    def finish_run_log(
        self,
        start_timestamp: str,
        status: str,
        new_articles: int,
        errors_count: int,
        dedup_removed: int,
        error_details: str,
        processed: str,
    ) -> None:
        """Дозаполняет строку, вставленную acquire_run_lock() в начале прогона.
        Строка ищется по точному совпадению временной метки, а не по фиксированному
        номеру - к этому моменту она могла сместиться вниз, если успел стартовать
        ещё один (не заблокированный) прогон.

        processed передаётся явно вызывающим кодом (main.py), а не выводится из status:
        "да", если все ошибки прогона не требуют вмешательства (сами исчезнут при
        следующем прогоне), "нет" - если хотя бы одна требует внимания."""
        worksheet = self._get_or_create_log_sheet()
        timestamps = worksheet.col_values(1)
        try:
            row_number = timestamps.index(start_timestamp) + 1
        except ValueError:
            row_number = 2  # не нашли (не должно происходить) - подстраховка

        label = LOG_STATUS_LABELS[status]
        worksheet.update(
            f"B{row_number}:G{row_number}",
            [[label, new_articles, errors_count, dedup_removed, error_details, processed]],
            value_input_option="USER_ENTERED",
        )
        self._format_log_row(worksheet, row_number, status)
        self._prune_log_sheet(worksheet)

    def write_fatal_log_row(self, error_text: str, processed: str = "нет") -> None:
        """Пишет полностью готовую fatal-строку за один раз - используется только когда
        не удалось даже настроиться (SheetsClient/чтение конфига), то есть до того, как
        успела произойти попытка acquire_run_lock()."""
        worksheet = self._get_or_create_log_sheet()
        timestamp = datetime.now(ZoneInfo("Europe/Kyiv")).strftime(LOG_TIMESTAMP_FORMAT)
        # RAW - см. комментарий в acquire_run_lock() про переформатирование метки времени.
        worksheet.insert_row(
            [timestamp, LOG_STATUS_LABELS["fatal"], 0, 1, 0, error_text, processed],
            index=2,
            value_input_option="RAW",
        )
        self._format_log_row(worksheet, 2, "fatal")
        self._prune_log_sheet(worksheet)

    def _set_row_background(self, worksheet: gspread.Worksheet, row_number: int, color: dict) -> None:
        self._spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": worksheet.id,
                                "startRowIndex": row_number - 1,
                                "endRowIndex": row_number,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(LOG_SHEET_HEADERS),
                            },
                            "cell": {"userEnteredFormat": {"backgroundColor": color}},
                            "fields": "userEnteredFormat.backgroundColor",
                        }
                    }
                ]
            }
        )

    def _format_log_row(self, worksheet: gspread.Worksheet, row_number: int, status: str) -> None:
        self._spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": worksheet.id,
                                "startRowIndex": row_number - 1,
                                "endRowIndex": row_number,
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
                                "startIndex": row_number - 1,
                                "endIndex": row_number,
                            }
                        }
                    },
                ]
            }
        )

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
