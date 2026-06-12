from __future__ import annotations

import calendar
from datetime import datetime, timezone
from typing import Any

import requests
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


def get_monthly_spend_usd(settings: Settings) -> float | None:
    """Read current month spend from the OpenAI Organization Costs API when available.

    The response shape has changed over time, so the parser intentionally accepts a
    few official variants and returns None instead of failing the app.
    """
    if not settings.openai_admin_key:
        return None

    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    last_day = calendar.monthrange(now.year, now.month)[1]
    end = datetime(now.year, now.month, last_day, 23, 59, 59, tzinfo=timezone.utc)

    headers = {"Authorization": f"Bearer {settings.openai_admin_key}"}
    params = {"start_time": int(start.timestamp()), "end_time": int(end.timestamp())}

    try:
        response = requests.get(
            "https://api.openai.com/v1/organization/costs",
            headers=headers,
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None

    return _sum_cost_payload(payload)


def _sum_cost_payload(payload: Any) -> float | None:
    total = 0.0
    found = False

    def walk(value: Any) -> None:
        nonlocal total, found
        if isinstance(value, dict):
            amount = value.get("amount")
            if isinstance(amount, dict) and isinstance(amount.get("value"), (int, float)):
                total += float(amount["value"])
                found = True
            elif isinstance(value.get("cost"), (int, float)):
                total += float(value["cost"])
                found = True
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return total if found else None

