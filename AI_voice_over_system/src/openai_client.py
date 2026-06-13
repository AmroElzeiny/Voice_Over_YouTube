from __future__ import annotations

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


def get_monthly_spend_status(settings: Settings) -> dict[str, Any]:
    """Read current-month organization spend and return a UI-friendly status."""
    if not settings.openai_admin_key:
        return {"amount_usd": None, "status": "admin_key_missing"}

    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    headers = {"Authorization": f"Bearer {settings.openai_admin_key}"}
    params: dict[str, Any] = {
        "start_time": int(start.timestamp()),
        "end_time": int(now.timestamp()) + 1,
        "bucket_width": "1d",
        "limit": 31,
    }
    total = 0.0
    found = False

    try:
        while True:
            response = requests.get(
                "https://api.openai.com/v1/organization/costs",
                headers=headers,
                params=params,
                timeout=20,
            )
            if response.status_code in {401, 403}:
                return {"amount_usd": None, "status": "admin_key_rejected"}
            response.raise_for_status()
            payload = response.json()
            page_total = _sum_cost_payload(payload)
            if page_total is not None:
                total += page_total
                found = True
            next_page = payload.get("next_page") if isinstance(payload, dict) else None
            if not (isinstance(payload, dict) and payload.get("has_more") and next_page):
                break
            params["page"] = next_page
    except requests.RequestException:
        return {"amount_usd": None, "status": "request_failed"}
    except (TypeError, ValueError):
        return {"amount_usd": None, "status": "invalid_response"}

    if not found:
        return {"amount_usd": 0.0, "status": "ok"}
    return {"amount_usd": total, "status": "ok"}


def get_monthly_spend_usd(settings: Settings) -> float | None:
    status = get_monthly_spend_status(settings)
    amount = status.get("amount_usd")
    return float(amount) if isinstance(amount, (int, float)) else None


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
