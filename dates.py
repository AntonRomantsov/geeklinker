from __future__ import annotations

import time

from dateutil import parser as date_parser

DATE_FORMAT = "%d.%m.%Y"


def format_date(raw: str = "", parsed_struct: time.struct_time | None = None) -> str:
    if parsed_struct is not None:
        return time.strftime(DATE_FORMAT, parsed_struct)

    if not raw:
        return ""

    try:
        return date_parser.parse(raw).strftime(DATE_FORMAT)
    except (ValueError, OverflowError):
        return raw
