from __future__ import annotations

import json
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import Config

EXTRACT_PROMPT = (
    "Извлеки список статей из этого содержимого. "
    "Для каждой статьи верни заголовок, дату размещения и полную ссылку.\n\n"
    "Содержимое:\n{content}"
)

RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "url": {"type": "string"},
            "pubDate": {"type": "string", "format": "date"},
        },
        "required": ["title", "url", "pubDate"],
    },
}

# 429 (RESOURCE_EXHAUSTED) и 5xx (сервер временно недоступен/перегружен) имеет смысл повторить;
# 400/401/403/404 — детерминированные ошибки, повтор их не исправит.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
RETRY_BACKOFF_SECONDS = (2, 5, 10)


def extract_articles(html_content: str, cfg: Config) -> list[dict]:
    client = genai.Client(api_key=cfg.gemini_api_key)
    attempts = len(RETRY_BACKOFF_SECONDS) + 1

    for attempt in range(attempts):
        try:
            response = client.models.generate_content(
                model=cfg.gemini_model,
                contents=EXTRACT_PROMPT.format(content=html_content),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                ),
            )
            return json.loads(response.text)
        except genai_errors.APIError as exc:
            is_last_attempt = attempt == attempts - 1
            if exc.code not in RETRYABLE_STATUS_CODES or is_last_attempt:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS[attempt])
        except json.JSONDecodeError:
            # Пустой/оборванный ответ — тоже транзиентная ситуация, стоит повторить.
            if attempt == attempts - 1:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS[attempt])
