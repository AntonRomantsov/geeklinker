from __future__ import annotations

import html
import time

import requests

from config import Config
from feeds import Article

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
SEND_DELAY_SECONDS = 0.4


def _escape_html(text: str) -> str:
    """Экранирует текст перед подстановкой в сообщение с parse_mode=HTML - без этого
    символы вроде <, >, & в заголовке статьи или тексте ошибки (например, попавший
    туда фрагмент HTML/JSON) ломают разбор на стороне Telegram и весь sendMessage
    падает с 400 Bad Request."""
    return html.escape(text, quote=True)


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
        f" 📄 <b>Новая статья на сайте {_escape_html(site_name)}</b>\n"
        f"Группа {_escape_html(group_name)}\n"
        f'   <a href="{_escape_html(item.link)}">{_escape_html(item.title)}</a>'
    )
    _send(cfg, cfg.telegram_chat_id, text)
    # Пауза, чтобы не словить 429 Too Many Requests при пачке новых статей подряд в один чат.
    time.sleep(SEND_DELAY_SECONDS)


def send_error_alert(cfg: Config, errors: list[str]) -> None:
    text = "⚠️ <b>GeekLinker: ошибки при обработке источников</b>\n\n" + "\n".join(
        f"• {_escape_html(error)}" for error in errors
    )
    _send(cfg, cfg.telegram_alert_chat_id, text)


def send_fatal_alert(cfg: Config, error_text: str) -> None:
    text = (
        "🔴 <b>GeekLinker: критическая ошибка</b>\n\n"
        f"{_escape_html(error_text)}\n\n"
        "Дальнейшая обработка в этом запуске невозможна."
    )
    _send(cfg, cfg.telegram_alert_chat_id, text)


def send_blocked_source_warning(cfg: Config, site_name: str, url: str) -> None:
    text = (
        f"🚫 <b>Источник «{_escape_html(site_name)}» не парсится</b>\n"
        f"{_escape_html(url)}\n\n"
        "robots.txt запрещает автоматический доступ и к RSS, и к обычной странице. "
        "Это не техническая ошибка, а осознанный отказ от источника — сайт явно не хочет "
        "автоматического парсинга. Обработка пропускается."
    )
    _send(cfg, cfg.telegram_alert_chat_id, text)
