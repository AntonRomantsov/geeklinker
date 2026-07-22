from __future__ import annotations

import re

import requests

from config import Config
from scheduler_admin import SchedulerAdmin, SchedulerError

API_BASE = "https://api.telegram.org/bot{token}/{method}"

EDIT_PROMPT_RE = re.compile(r"график «([^»]+)»")

STATE_LABELS = {
    1: "▶ активен",  # ENABLED
    2: "⏸ на паузе",  # PAUSED
    3: "🚫 отключён",  # DISABLED
    4: "⚠ ошибка",  # UPDATE_FAILED
}

HELP_TEXT = (
    "<b>Управление графиками</b>\n\n"
    "/menu — список графиков и кнопки управления\n\n"
    "/newjob &lt;имя&gt;\n&lt;cron-выражение&gt;\n&lt;таймзона, необязательно&gt;\n"
    "— создать новый график (макс. 3)\n\n"
    "/editjob &lt;имя&gt;\n&lt;новый cron&gt;\n&lt;новая таймзона, необязательно&gt;\n"
    "— изменить расписание существующего графика\n\n"
    "Пример:\n<code>/newjob evening\n0 21 * * *\nEurope/Kyiv</code>"
)


def _call(cfg: Config, method: str, payload: dict) -> dict:
    url = API_BASE.format(token=cfg.telegram_bot_token, method=method)
    response = requests.post(url, json=payload, timeout=15)
    response.raise_for_status()
    return response.json()


def _send_message(cfg: Config, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _call(cfg, "sendMessage", payload)


def _answer_callback(cfg: Config, callback_query_id: str, text: str = "") -> None:
    _call(cfg, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


def _short_name(job) -> str:
    return job.name.rsplit("/", 1)[-1]


def _job_line(job) -> str:
    state_label = STATE_LABELS.get(int(job.state), str(job.state))
    return f"<b>{_short_name(job)}</b>\n{job.schedule} ({job.time_zone}) — {state_label}"


def _menu_keyboard(jobs) -> dict:
    rows = []
    for job in jobs:
        name = _short_name(job)
        is_paused = int(job.state) == 2
        toggle = {"text": "▶️ Возобновить", "callback_data": f"resume|{name}"} if is_paused else {
            "text": "⏸ Пауза",
            "callback_data": f"pause|{name}",
        }
        rows.append([toggle, {"text": "✏️ Изменить", "callback_data": f"editstart|{name}"}])
        rows.append(
            [
                {"text": "⚡ Запустить", "callback_data": f"run|{name}"},
                {"text": "🗑 Удалить", "callback_data": f"delconfirm|{name}"},
            ]
        )
    if len(jobs) < 3:
        rows.append([{"text": "➕ Новый график (см. /newjob)", "callback_data": "noop"}])
    return {"inline_keyboard": rows}


def _send_menu(cfg: Config, chat_id: int) -> None:
    admin = SchedulerAdmin(cfg)
    jobs = admin.list_jobs()
    if not jobs:
        _send_message(cfg, chat_id, "Графиков пока нет. Создайте через /newjob.")
        return
    text = f"<b>Графики ({len(jobs)}/3):</b>\n\n" + "\n\n".join(_job_line(j) for j in jobs)
    _send_message(cfg, chat_id, text, _menu_keyboard(jobs))


def _parse_multiline_command(text: str) -> list[str]:
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    return lines


def _handle_command(cfg: Config, chat_id: int, text: str) -> None:
    stripped = text.strip()
    command_line, *_ = stripped.split("\n", 1) + [""]
    command = command_line.split()[0].split("@")[0].lower()

    if command in ("/start", "/help"):
        _send_message(cfg, chat_id, HELP_TEXT)
        return

    if command == "/menu":
        _send_menu(cfg, chat_id)
        return

    if command == "/newjob":
        lines = _parse_multiline_command(stripped)
        first_line_rest = lines[0].split(maxsplit=1)
        name = first_line_rest[1] if len(first_line_rest) > 1 else None
        rest = lines[1:]
        if not name and rest:
            name, *rest = rest
        if not name or not rest:
            _send_message(cfg, chat_id, "Формат:\n<code>/newjob имя\ncron\nтаймзона(необязательно)</code>")
            return
        cron = rest[0]
        timezone = rest[1] if len(rest) > 1 else "Europe/Kyiv"
        try:
            SchedulerAdmin(cfg).create_job(name, cron, timezone)
            _send_message(cfg, chat_id, f"✅ График «{name}» создан.")
            _send_menu(cfg, chat_id)
        except SchedulerError as exc:
            _send_message(cfg, chat_id, f"❌ {exc}")
        return

    if command == "/editjob":
        lines = _parse_multiline_command(stripped)
        first_line_rest = lines[0].split(maxsplit=1)
        name = first_line_rest[1] if len(first_line_rest) > 1 else None
        rest = lines[1:]
        if not name and rest:
            name, *rest = rest
        if not name or not rest:
            _send_message(
                cfg, chat_id, "Формат:\n<code>/editjob имя\nновый cron\nновая таймзона(необязательно)</code>"
            )
            return
        cron = rest[0]
        timezone = rest[1] if len(rest) > 1 else None
        try:
            SchedulerAdmin(cfg).update_schedule(name, cron, timezone)
            _send_message(cfg, chat_id, f"✅ График «{name}» обновлён.")
            _send_menu(cfg, chat_id)
        except SchedulerError as exc:
            _send_message(cfg, chat_id, f"❌ {exc}")
        return

    _send_message(cfg, chat_id, "Неизвестная команда. /help — список команд.")


def _handle_edit_reply(cfg: Config, chat_id: int, name: str, text: str) -> None:
    lines = _parse_multiline_command(text)
    if not lines:
        _send_message(cfg, chat_id, "Пустой ответ, изменение отменено.")
        return
    cron = lines[0]
    timezone = lines[1] if len(lines) > 1 else None
    try:
        SchedulerAdmin(cfg).update_schedule(name, cron, timezone)
        _send_message(cfg, chat_id, f"✅ График «{name}» обновлён.")
    except SchedulerError as exc:
        _send_message(cfg, chat_id, f"❌ {exc}")
    _send_menu(cfg, chat_id)


def _handle_callback(cfg: Config, callback_query: dict) -> None:
    chat_id = callback_query["message"]["chat"]["id"]
    callback_id = callback_query["id"]
    data = callback_query.get("data", "")

    if data == "noop":
        _answer_callback(cfg, callback_id, "Используйте команду /newjob")
        return

    action, _, name = data.partition("|")
    admin = SchedulerAdmin(cfg)

    try:
        if action == "pause":
            admin.pause_job(name)
            _answer_callback(cfg, callback_id, f"Пауза: {name}")
        elif action == "resume":
            admin.resume_job(name)
            _answer_callback(cfg, callback_id, f"Возобновлено: {name}")
        elif action == "run":
            admin.run_job(name)
            _answer_callback(cfg, callback_id, f"Запущено: {name}")
        elif action == "editstart":
            _answer_callback(cfg, callback_id)
            _send_message(
                cfg,
                chat_id,
                f"Новый график для «{name}» — ответьте на это сообщение.\n"
                "Первая строка — cron, вторая (необязательно) — таймзона.",
                {"force_reply": True},
            )
            return
        elif action == "delconfirm":
            _answer_callback(cfg, callback_id)
            _send_message(
                cfg,
                chat_id,
                f"Удалить график «{name}»?",
                {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Да, удалить", "callback_data": f"delete|{name}"},
                            {"text": "❌ Отмена", "callback_data": "noop"},
                        ]
                    ]
                },
            )
            return
        elif action == "delete":
            admin.delete_job(name)
            _answer_callback(cfg, callback_id, f"Удалено: {name}")
        else:
            _answer_callback(cfg, callback_id, "Неизвестное действие")
            return
    except SchedulerError as exc:
        _answer_callback(cfg, callback_id, str(exc)[:200])
        return

    _send_menu(cfg, chat_id)


def handle_update(cfg: Config, update: dict) -> None:
    message = update.get("message")
    callback_query = update.get("callback_query")

    sender_chat_id = None
    if message:
        sender_chat_id = message.get("chat", {}).get("id")
    elif callback_query:
        sender_chat_id = callback_query.get("message", {}).get("chat", {}).get("id")

    if sender_chat_id is None or str(sender_chat_id) != str(cfg.telegram_chat_id):
        return  # игнорируем сообщения не от владельца бота

    if message and "text" in message:
        reply_to = message.get("reply_to_message") or {}
        edit_match = EDIT_PROMPT_RE.search(reply_to.get("text", ""))
        if edit_match:
            _handle_edit_reply(cfg, sender_chat_id, edit_match.group(1), message["text"])
        else:
            _handle_command(cfg, sender_chat_id, message["text"])
    elif callback_query:
        _handle_callback(cfg, callback_query)
