from __future__ import annotations

import time

import requests

from config import Config
from feeds import Article

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
SEND_DELAY_SECONDS = 0.4


def _send(cfg: Config, chat_id: str, text: str) -> None:
    response = requests.post(
        TELEGRAM_API.format(token=cfg.telegram_bot_token),
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    response.raise_for_status()


def send_new_article(cfg: Config, site_name: str, group_name: str, item: Article) -> None:
    text = (
        f" 📄 <b>Новая статья на сайте {site_name}</b>\n"
        f"Группа {group_name}\n"
        f'   <a href="{item.link}">{item.title}</a>'
    )
    _send(cfg, cfg.telegram_chat_id, text)
    # Пауза, чтобы не словить 429 Too Many Requests при пачке новых статей подряд в один чат.
    time.sleep(SEND_DELAY_SECONDS)


def send_error_alert(cfg: Config, errors: list[str]) -> None:
    text = "⚠️ <b>GeekLinker: ошибки при обработке источников</b>\n\n" + "\n".join(
        f"• {error}" for error in errors
    )
    _send(cfg, cfg.telegram_alert_chat_id, text)


def send_fatal_alert(cfg: Config, error_text: str) -> None:
    text = (
        "🔴 <b>GeekLinker: критическая ошибка</b>\n\n"
        f"{error_text}\n\n"
        "Дальнейшая обработка в этом запуске невозможна."
    )
    _send(cfg, cfg.telegram_alert_chat_id, text)


def send_blocked_source_warning(cfg: Config, site_name: str, url: str) -> None:
    text = (
        f"🚫 <b>Источник «{site_name}» не парсится</b>\n"
        f"{url}\n\n"
        "robots.txt запрещает автоматический доступ и к RSS, и к обычной странице. "
        "Это не техническая ошибка, а осознанный отказ от источника — сайт явно не хочет "
        "автоматического парсинга. Обработка пропускается."
    )
    _send(cfg, cfg.telegram_alert_chat_id, text)
