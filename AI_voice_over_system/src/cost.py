from __future__ import annotations

from typing import Any

import tiktoken

from .config import Settings


def estimate_tokens(text: str, model: str = "gpt-4o-mini") -> int:
    try:
        encoding = tiktoken.encoding_for_model(model)
    except Exception:
        encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text or ""))


def estimate_from_minutes(settings: Settings, minutes: float, language_count: int) -> dict[str, Any]:
    minutes = max(0.1, float(minutes or 0.1))
    language_count = max(1, language_count)
    estimated_source_tokens = int(minutes * 140)
    return estimate_cost(
        settings=settings,
        duration_seconds=minutes * 60,
        source_token_count=estimated_source_tokens,
        target_language_count=language_count,
    )


def estimate_from_segments(settings: Settings, segments: list[dict], target_language_count: int) -> dict[str, Any]:
    text = "\n".join(str(segment.get("source_text") or "") for segment in segments)
    duration = max((float(segment.get("end", 0.0)) for segment in segments), default=0.0)
    source_tokens = estimate_tokens(text, settings.openai_text_model)
    return estimate_cost(settings, duration, source_tokens, target_language_count)


def estimate_cost(
    settings: Settings,
    duration_seconds: float,
    source_token_count: int,
    target_language_count: int,
) -> dict[str, Any]:
    minutes = max(0.1, duration_seconds / 60)
    language_count = max(1, target_language_count)
    source_token_count = max(1, source_token_count)

    transcription = minutes * settings.whisper_1_usd_per_min
    translation_input_tokens = (source_token_count + 700) * language_count
    translation_output_tokens = int(source_token_count * 1.25) * language_count
    translation = (
        translation_input_tokens / 1_000_000 * settings.gpt_4o_mini_input_usd_per_1m
        + translation_output_tokens / 1_000_000 * settings.gpt_4o_mini_output_usd_per_1m
    )

    tts_text_tokens = int(source_token_count * 1.20) * language_count
    tts_text = tts_text_tokens / 1_000_000 * settings.gpt_4o_mini_tts_text_input_usd_per_1m
    tts_audio = minutes * language_count * settings.openai_tts_estimated_usd_per_min
    tts = tts_text + tts_audio

    subtotal = transcription + translation + tts
    safety_buffer = subtotal * (settings.cost_safety_buffer_percent / 100)
    total = subtotal + safety_buffer

    return {
        "is_estimate": True,
        "duration_minutes": minutes,
        "language_count": language_count,
        "source_tokens_estimated": source_token_count,
        "translation_input_tokens_estimated": translation_input_tokens,
        "translation_output_tokens_estimated": translation_output_tokens,
        "tts_text_tokens_estimated": tts_text_tokens,
        "tts_audio_usd_estimated": tts_audio,
        "transcription_usd": transcription,
        "translation_usd": translation,
        "tts_usd": tts,
        "safety_buffer_usd": safety_buffer,
        "total_usd": total,
    }


def translation_usage_cost(settings: Settings, prompt_tokens: int, completion_tokens: int) -> float:
    return (
        prompt_tokens / 1_000_000 * settings.gpt_4o_mini_input_usd_per_1m
        + completion_tokens / 1_000_000 * settings.gpt_4o_mini_output_usd_per_1m
    )


def transcription_duration_cost(settings: Settings, duration_seconds: float) -> float:
    return max(0.0, duration_seconds) / 60 * settings.whisper_1_usd_per_min


def tts_token_usage_cost(settings: Settings, input_tokens: int, output_audio_tokens: int) -> float:
    return (
        max(0, input_tokens) / 1_000_000 * settings.gpt_4o_mini_tts_text_input_usd_per_1m
        + max(0, output_audio_tokens)
        / 1_000_000
        * settings.gpt_4o_mini_tts_audio_output_usd_per_1m
    )


def recorded_jobs_summary(
    job_records: list[dict[str, Any]],
    additional_usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    total_usd = 0.0
    total_tokens = 0
    transcription_minutes = 0.0
    counted_jobs = 0

    for job in job_records:
        actual = job.get("actual_cost_json") or {}
        job_cost = actual.get("total_usd")
        if job_cost is None:
            job_cost = actual.get("total_known_plus_estimated_usd")
        if job_cost is None and job.get("status") == "completed":
            estimate = job.get("estimated_cost_json") or {}
            subtotal = sum(
                float(estimate.get(key) or 0)
                for key in ("transcription_usd", "translation_usd", "tts_usd")
            )
            job_cost = subtotal if subtotal > 0 else None
        if job_cost is None:
            continue

        counted_jobs += 1
        total_usd += float(job_cost)
        total_tokens += int(actual.get("total_billable_tokens") or actual.get("translation_total_tokens") or 0)
        transcription_minutes += float(actual.get("transcription_minutes") or 0)

    if isinstance(additional_usage, dict):
        total_usd += float(additional_usage.get("cost_usd") or 0)
        total_tokens += int(additional_usage.get("total_tokens") or 0)

    return {
        "job_count": counted_jobs,
        "total_usd": total_usd,
        "total_billable_tokens": total_tokens,
        "transcription_minutes": transcription_minutes,
    }


def supposed_balance(settings: Settings, recorded_cost_usd: float) -> float | None:
    """Return the configured opening balance minus usage recorded by this app."""
    if settings.openai_manual_available_balance_usd is None:
        return None
    return max(0.0, settings.openai_manual_available_balance_usd - max(0.0, recorded_cost_usd))


def budget_status(
    settings: Settings,
    estimated_cost: dict[str, Any],
    monthly_spend_usd: float | None,
    recorded_cost_usd: float = 0.0,
) -> dict[str, Any]:
    estimated = float(estimated_cost.get("total_usd") or 0)
    if settings.openai_manual_available_balance_usd is not None:
        available = supposed_balance(settings, recorded_cost_usd) or 0.0
        return {
            "available_usd": available,
            "allowed": estimated <= available,
            "source": "calculated_manual_balance",
        }

    if settings.openai_monthly_budget_usd is not None and monthly_spend_usd is not None:
        available = settings.openai_monthly_budget_usd - monthly_spend_usd
        return {
            "available_usd": available,
            "allowed": estimated <= available,
            "source": "monthly_budget",
        }

    return {"available_usd": None, "allowed": True, "source": "none"}
