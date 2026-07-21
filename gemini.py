from __future__ import annotations

import json

from google import genai
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


def extract_articles(html_content: str, cfg: Config) -> list[dict]:
    client = genai.Client(api_key=cfg.gemini_api_key)
    response = client.models.generate_content(
        model=cfg.gemini_model,
        contents=EXTRACT_PROMPT.format(content=html_content),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
        ),
    )
    return json.loads(response.text)
