import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


@dataclass
class Config:
    spreadsheet_id: str
    config_sheet_name: str
    dedup_sheet_name: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_alert_chat_id: str
    gemini_api_key: str
    gemini_model: str
    scrape_cookie_header: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            spreadsheet_id=_require("SPREADSHEET_ID"),
            config_sheet_name=os.environ.get("CONFIG_SHEET_NAME", "blog_config"),
            dedup_sheet_name=os.environ.get("DEDUP_SHEET_NAME", "_Dedup"),
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_require("TELEGRAM_CHAT_ID"),
            telegram_alert_chat_id=os.environ.get("TELEGRAM_ALERT_CHAT_ID") or _require("TELEGRAM_CHAT_ID"),
            gemini_api_key=_require("GEMINI_API_KEY"),
            gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
            scrape_cookie_header=os.environ.get("SCRAPE_COOKIE_HEADER", "user_lang_select_force=ru"),
        )


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения {name}")
    return value
