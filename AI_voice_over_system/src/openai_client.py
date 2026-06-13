from __future__ import annotations

from typing import Any

from openai import OpenAI

from .config import Settings


class OpenAIConfigError(RuntimeError):
    pass


def get_client(settings: Settings) -> OpenAI:
    if not settings.openai_api_key:
        raise OpenAIConfigError(
            "مفتاح OpenAI غير موجود. أضف OPENAI_API_KEY في Streamlit secrets أو ملف .env."
        )
    return OpenAI(api_key=settings.openai_api_key)


def response_to_dict(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if isinstance(response, dict):
        return response
    result: dict[str, Any] = {}
    for key in dir(response):
        if key.startswith("_"):
            continue
        try:
            value = getattr(response, key)
        except Exception:
            continue
        if not callable(value):
            result[key] = value
    return result
