from __future__ import annotations

import time
from datetime import date

from dateutil import parser as date_parser

DATE_FORMAT = "%d.%m.%Y"


def format_date(raw: str = "", parsed_struct: time.struct_time | None = None) -> str:
    if parsed_struct is not None:
        return time.strftime(DATE_FORMAT, parsed_struct)

    if not raw:
        # Дата не указана источником — ставим текущую, чтобы не оставлять пустую ячейку.
        return date.today().strftime(DATE_FORMAT)

    try:
        return date_parser.parse(raw).strftime(DATE_FORMAT)
    except (ValueError, OverflowError):
        return raw
