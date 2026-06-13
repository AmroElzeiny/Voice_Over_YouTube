from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Any

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from . import subtitles
from .config import (
    DEFAULT_TRANSLATION_PROMPT_TEMPLATE,
    DEFAULT_TRANSLATION_REPAIR_PROMPT_TEMPLATE,
    Settings,
)
from .openai_client import get_client, response_to_dict
from .logging_utils import log_event
from .storage import read_json, write_json

ProgressCallback = Callable[[str, float], None]
CancelCallback = Callable[[], None]
UsageCallback = Callable[[dict[str, int]], None]
TRANSLATION_ENGINE_VERSION = "segment-recovery-v2"

LANGUAGE_INSTRUCTIONS = {
    "zh": "Use Simplified Chinese and natural Mandarin phrasing.",
    "hi": "Use natural spoken Hindi that sounds fluent when read aloud.",
    "ms": "Use natural standard Malay suitable for spoken narration.",
    "id": "Use natural Bahasa Indonesia suitable for spoken narration.",
}


def _is_retryable(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return any(token in name or token in message for token in ("rate", "timeout", "connection", "temporar", "server"))


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _usage_dict(response: Any) -> dict[str, int]:
    payload = response_to_dict(response)
    usage = payload.get("usage") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _content_from_response(response: Any) -> str:
    payload = response_to_dict(response)
    choices = payload.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        return str(message.get("content") or "")
    return ""


def _render_template(template: str, fallback: str, **values: Any) -> str:
    try:
        return template.format(**values)
    except Exception:
        return fallback.format(**values)


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _chat_json(client, model: str, messages: list[dict[str, str]]):
    try:
        return client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except TypeError:
        return client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.2,
        )


def _parse_translation(
    raw: str,
    expected_ids: list[int],
    *,
    allow_single_reassignment: bool = False,
) -> tuple[dict[int, str], list[int], list[int], list[int]]:
    payload = json.loads(_strip_code_fence(raw))
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise ValueError("Translation JSON schema is invalid.")

    result: dict[int, str] = {}
    duplicate_ids: list[int] = []
    nonempty_items: list[tuple[int, str]] = []
    for item in payload["segments"]:
        if not isinstance(item, dict):
            continue
        try:
            segment_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        target_text = str(item.get("target_text") or "").strip()
        if not target_text:
            continue
        nonempty_items.append((segment_id, target_text))
        if segment_id in result:
            duplicate_ids.append(segment_id)
            continue
        result[segment_id] = target_text

    expected_set = set(expected_ids)
    if allow_single_reassignment and len(expected_ids) == 1 and expected_ids[0] not in result and len(nonempty_items) == 1:
        result[expected_ids[0]] = nonempty_items[0][1]

    unexpected_ids = sorted(segment_id for segment_id in result if segment_id not in expected_set)
    valid_result = {segment_id: text for segment_id, text in result.items() if segment_id in expected_set}
    missing_ids = [segment_id for segment_id in expected_ids if segment_id not in valid_result]
    return valid_result, missing_ids, unexpected_ids, sorted(set(duplicate_ids))


def _add_usage(total: dict[str, int], usage: dict[str, int]) -> None:
    for key in total:
        total[key] += int(usage.get(key) or 0)


def _translation_messages(
    settings: Settings,
    lang_code: str,
    target_language: str,
    segments: list[dict],
    *,
    recovery: bool = False,
) -> list[dict[str, str]]:
    segments_json = json.dumps(
        [{"id": int(segment["id"]), "text": segment.get("source_text", "")} for segment in segments],
        ensure_ascii=False,
    )
    prompt = _render_template(
        settings.translation_prompt_template,
        DEFAULT_TRANSLATION_PROMPT_TEMPLATE,
        target_language=target_language,
        segments_json=segments_json,
        language_instruction=LANGUAGE_INSTRUCTIONS.get(
            lang_code, "Use natural phrasing appropriate for native speakers."
        ),
    )
    prompt += (
        "\n\nMandatory voiceover rules:\n"
        "- Keep the translation natural and easy to speak aloud.\n"
        "- Avoid unnecessary sentence expansion and prefer concise native phrasing.\n"
        "- Do not make the result robotic.\n"
        f"- {LANGUAGE_INSTRUCTIONS.get(lang_code, 'Use natural phrasing appropriate for native speakers.')}"
    )
    if recovery:
        expected_ids = [int(segment["id"]) for segment in segments]
        recovery_instruction = _render_template(
            settings.translation_repair_prompt_template,
            DEFAULT_TRANSLATION_REPAIR_PROMPT_TEMPLATE,
            expected_ids=json.dumps(expected_ids),
        )
        prompt += (
            "\n\nThis is a recovery request. Return every listed segment exactly once. "
            f"The only permitted IDs are: {json.dumps(expected_ids)}."
        )
        system_prompt = f"{settings.translation_system_prompt}\n{recovery_instruction}"
    else:
        system_prompt = settings.translation_system_prompt
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]


def _request_translation(
    client,
    settings: Settings,
    lang_code: str,
    target_language: str,
    segments: list[dict],
    *,
    recovery: bool = False,
    allow_single_reassignment: bool = False,
) -> tuple[dict[int, str], list[int], list[int], list[int], dict[str, int]]:
    response = _chat_json(
        client,
        settings.openai_text_model,
        _translation_messages(settings, lang_code, target_language, segments, recovery=recovery),
    )
    content = _content_from_response(response)
    expected_ids = [int(segment["id"]) for segment in segments]
    usage = _usage_dict(response)
    try:
        result, missing_ids, unexpected_ids, duplicate_ids = _parse_translation(
            content,
            expected_ids,
            allow_single_reassignment=allow_single_reassignment,
        )
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}, expected_ids, [], [], usage
    return result, missing_ids, unexpected_ids, duplicate_ids, usage


def _recover_missing_translations(
    client,
    settings: Settings,
    job_path: Path,
    source_by_id: dict[int, dict],
    missing_ids: list[int],
    lang_code: str,
    target_language: str,
    usage_total: dict[str, int],
    cancel_check: CancelCallback | None,
) -> dict[int, str]:
    recovered: dict[int, str] = {}
    recovery_size = settings.translation_recovery_batch_size

    for start in range(0, len(missing_ids), recovery_size):
        if cancel_check:
            cancel_check()
        group_ids = missing_ids[start : start + recovery_size]
        group = [source_by_id[segment_id] for segment_id in group_ids]
        try:
            result, still_missing, unexpected, duplicates, usage = _request_translation(
                client,
                settings,
                lang_code,
                target_language,
                group,
                recovery=True,
                allow_single_reassignment=len(group) == 1,
            )
            _add_usage(usage_total, usage)
            recovered.update(result)
            if still_missing or unexpected or duplicates:
                log_event(
                    settings,
                    "translation_recovery_mismatch",
                    "A recovery response still had segment ID problems; unresolved segments will be retried individually.",
                    level="WARNING",
                    job_id=job_path.name,
                    job_path=job_path,
                    language=lang_code,
                    missing_ids=still_missing,
                    unexpected_ids=unexpected,
                    duplicate_ids=duplicates,
                )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            log_event(
                settings,
                "translation_recovery_invalid_json",
                str(exc),
                level="WARNING",
                job_id=job_path.name,
                job_path=job_path,
                language=lang_code,
                requested_ids=group_ids,
            )

    unresolved = [segment_id for segment_id in missing_ids if segment_id not in recovered]
    for segment_id in unresolved:
        segment = source_by_id[segment_id]
        for attempt in range(2):
            if cancel_check:
                cancel_check()
            try:
                result, still_missing, _unexpected, _duplicates, usage = _request_translation(
                    client,
                    settings,
                    lang_code,
                    target_language,
                    [segment],
                    recovery=True,
                    allow_single_reassignment=True,
                )
                _add_usage(usage_total, usage)
                if not still_missing and segment_id in result:
                    recovered[segment_id] = result[segment_id]
                    break
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                log_event(
                    settings,
                    "translation_single_segment_retry",
                    str(exc),
                    level="WARNING",
                    job_id=job_path.name,
                    job_path=job_path,
                    language=lang_code,
                    segment_id=segment_id,
                    attempt=attempt + 1,
                )

    return recovered


def translate_segments(
    settings: Settings,
    job_path: Path,
    segments: list[dict],
    lang_code: str,
    target_language: str,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCallback | None = None,
    usage_update: UsageCallback | None = None,
) -> tuple[list[dict], dict[str, int]]:
    translation_path = job_path / f"translation_{lang_code}.json"
    existing = read_json(translation_path)
    if isinstance(existing, dict) and existing.get("segments"):
        subtitles.write_srt(job_path / f"translation_{lang_code}.srt", existing["segments"], text_field="target_text")
        if usage_update:
            usage_update(existing.get("usage", {}))
        return existing["segments"], existing.get("usage", {})

    client = get_client(settings)
    batch_size = settings.translation_batch_size
    progress_path = job_path / f"translation_{lang_code}_progress.json"
    source_ids = [int(segment["id"]) for segment in segments]
    source_by_id = {int(segment["id"]): segment for segment in segments}
    translated_by_id: dict[int, str] = {}
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    log_event(
        settings,
        "translation_engine_started",
        "Translation started with targeted segment-ID recovery.",
        job_id=job_path.name,
        job_path=job_path,
        language=lang_code,
        engine_version=TRANSLATION_ENGINE_VERSION,
        segment_count=len(segments),
        batch_size=batch_size,
        recovery_batch_size=settings.translation_recovery_batch_size,
    )

    saved_progress = read_json(progress_path, {})
    if (
        isinstance(saved_progress, dict)
        and saved_progress.get("source_ids") == source_ids
        and saved_progress.get("model") == settings.openai_text_model
    ):
        for raw_id, text in (saved_progress.get("translations") or {}).items():
            try:
                segment_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if segment_id in source_by_id and str(text).strip():
                translated_by_id[segment_id] = str(text).strip()
        saved_usage = saved_progress.get("usage") or {}
        for key in usage_total:
            usage_total[key] = int(saved_usage.get(key) or 0)
        if translated_by_id:
            log_event(
                settings,
                "translation_progress_resumed",
                "Saved translation progress was loaded.",
                job_id=job_path.name,
                job_path=job_path,
                language=lang_code,
                completed_segments=len(translated_by_id),
            )
        if usage_update and any(usage_total.values()):
            usage_update(dict(usage_total))

    for batch_start in range(0, len(segments), batch_size):
        if cancel_check:
            cancel_check()
        batch = segments[batch_start : batch_start + batch_size]
        expected_ids = [int(segment["id"]) for segment in batch]
        if all(segment_id in translated_by_id for segment_id in expected_ids):
            continue
        if progress:
            progress(
                f"ترجمة {target_language}: المجموعة {batch_start // batch_size + 1}",
                batch_start / max(1, len(segments)),
            )
        pending_batch = [segment for segment in batch if int(segment["id"]) not in translated_by_id]
        pending_ids = [int(segment["id"]) for segment in pending_batch]
        try:
            batch_result, missing_ids, unexpected_ids, duplicate_ids, usage = _request_translation(
                client,
                settings,
                lang_code,
                target_language,
                pending_batch,
            )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            batch_result = {}
            missing_ids = pending_ids
            unexpected_ids = []
            duplicate_ids = []
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            log_event(
                settings,
                "translation_batch_invalid_json",
                str(exc),
                level="WARNING",
                job_id=job_path.name,
                job_path=job_path,
                language=lang_code,
                expected_ids=pending_ids,
            )
        if cancel_check:
            cancel_check()
        _add_usage(usage_total, usage)
        if missing_ids or unexpected_ids or duplicate_ids:
            log_event(
                settings,
                "translation_id_mismatch",
                "The model returned incomplete or unexpected segment IDs; starting targeted recovery.",
                level="WARNING",
                job_id=job_path.name,
                job_path=job_path,
                language=lang_code,
                missing_ids=missing_ids,
                unexpected_ids=unexpected_ids,
                duplicate_ids=duplicate_ids,
            )
        batch_result.update(
            _recover_missing_translations(
                client,
                settings,
                job_path,
                source_by_id,
                missing_ids,
                lang_code,
                target_language,
                usage_total,
                cancel_check,
            )
        )
        unresolved = [segment_id for segment_id in pending_ids if segment_id not in batch_result]
        if unresolved:
            raise ValueError(
                f"Translation recovery failed for {len(unresolved)} segment(s): {unresolved[:20]}"
            )
        translated_by_id.update(batch_result)
        write_json(
            progress_path,
            {
                "model": settings.openai_text_model,
                "language": lang_code,
                "source_ids": source_ids,
                "translations": {str(key): value for key, value in translated_by_id.items()},
                "usage": usage_total,
            },
        )
        if usage_update:
            usage_update(dict(usage_total))

    translated_segments: list[dict] = []
    for segment in segments:
        segment_id = int(segment["id"])
        translated_segments.append(
            {
                **segment,
                "target_text": translated_by_id[segment_id],
                "language": lang_code,
            }
        )

    write_json(
        translation_path,
        {
            "model": settings.openai_text_model,
            "language": lang_code,
            "segments": translated_segments,
            "usage": usage_total,
        },
    )
    subtitles.write_srt(job_path / f"translation_{lang_code}.srt", translated_segments, text_field="target_text")
    progress_path.unlink(missing_ok=True)
    if progress:
        progress(f"اكتملت ترجمة {target_language}", 1.0)
    if usage_update:
        usage_update(dict(usage_total))
    return translated_segments, usage_total
