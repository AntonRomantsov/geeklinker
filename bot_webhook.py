from __future__ import annotations

import functions_framework

from config import Config
from telegram_bot import handle_update


@functions_framework.http
def telegram_webhook(request):
    cfg = Config.from_env()

    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not cfg.telegram_webhook_secret or secret_header != cfg.telegram_webhook_secret:
        return ("forbidden", 403)

    update = request.get_json(silent=True) or {}
    handle_update(cfg, update)
    return ("ok", 200)
