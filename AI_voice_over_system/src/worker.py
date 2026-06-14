from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from . import chunking, cost, jobs, media, transcription, translation, tts, youtube
from .config import LANGUAGES, Settings
from .logging_utils import log_event
from .storage import (
    collect_language_outputs,
    find_first_file,
    job_dir,
    logs_dir,
    read_json,
    source_dir,
    write_json,
)

_WORKERS: dict[str, threading.Thread] = {}
_WORKER_LOCK = threading.Lock()


class JobCancelled(RuntimeError):
    """Raised cooperatively when the user requests cancellation."""


class BudgetBlocked(RuntimeError):
    """Raised when the configured app balance cannot cover the real estimate."""


def check_cancelled(settings: Settings, job_id: str) -> None:
    if jobs.is_cancel_requested(settings, job_id):
        log_event(settings, "job_cancel_detected", "Cancellation request detected.", job_id=job_id)
        raise JobCancelled("Job cancellation requested.")


def start_job_worker(settings: Settings, job_id: str) -> bool:
    with _WORKER_LOCK:
        existing = _WORKERS.get(job_id)
        if existing and existing.is_alive():
            return False
        thread = threading.Thread(target=run_job, args=(settings, job_id), daemon=True)
        _WORKERS[job_id] = thread
        thread.start()
        return True


def is_worker_alive(job_id: str) -> bool:
    thread = _WORKERS.get(job_id)
    return bool(thread and thread.is_alive())


def _checkpoint(path: Path, name: str, data: dict[str, Any] | None = None) -> None:
    checkpoints = read_json(path / "checkpoints.json", {})
    checkpoints[name] = {"done": True, **(data or {})}
    write_json(path / "checkpoints.json", checkpoints)


def _progress(settings: Settings, job_id: str, base: float, span: float) -> Callable[[str, float], None]:
    def inner(step: str, local_percent: float) -> None:
        check_cancelled(settings, job_id)
        jobs.set_progress(settings, job_id, step, base + span * max(0.0, min(1.0, local_percent)))

    return inner


def _load_source(settings: Settings, job: dict[str, Any], path: Path, log_path: Path) -> Path:
    src_dir = source_dir(settings, job["job_id"])
    config = job.get("config_json") or {}
    if job["input_type"] == "youtube":
        existing = find_first_file(src_dir, ["youtube_source.*"])
        if existing:
            return existing
        return youtube.download_youtube_audio(
            job["source_name_or_url"],
            src_dir,
            settings,
            log_path,
        )

    source_file = config.get("source_file")
    if source_file:
        candidate = src_dir / source_file
        if candidate.exists():
            return candidate
    existing_upload = find_first_file(src_dir, ["*"])
    if existing_upload:
        return existing_upload
    raise FileNotFoundError("ملف الرفع غير موجود داخل مجلد العملية.")


def _update_actual_translation_cost(
    settings: Settings,
    job_id: str,
    lang_code: str,
    usage: dict[str, int],
) -> dict[str, Any]:
    job = jobs.get_job(settings, job_id) or {}
    actual = job.get("actual_cost_json") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    by_language = actual.setdefault("translation_by_language", {})
    by_language[lang_code] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": int(usage.get("total_tokens") or prompt_tokens + completion_tokens),
        "cost_usd": cost.translation_usage_cost(settings, prompt_tokens, completion_tokens),
    }
    actual["translation_prompt_tokens"] = sum(
        int(item.get("prompt_tokens") or 0) for item in by_language.values()
    )
    actual["translation_completion_tokens"] = sum(
        int(item.get("completion_tokens") or 0) for item in by_language.values()
    )
    actual["translation_total_tokens"] = sum(
        int(item.get("total_tokens") or 0) for item in by_language.values()
    )
    actual["translation_usd"] = sum(float(item.get("cost_usd") or 0) for item in by_language.values())
    _recalculate_actual_totals(actual)
    jobs.update_job(settings, job_id, actual_cost_json=actual)
    return actual


def _recalculate_actual_totals(actual: dict[str, Any]) -> None:
    actual["tts_input_tokens"] = sum(
        int(item.get("input_tokens") or 0) for item in (actual.get("tts_by_language") or {}).values()
    )
    actual["tts_output_audio_tokens"] = sum(
        int(item.get("output_audio_tokens") or 0) for item in (actual.get("tts_by_language") or {}).values()
    )
    actual["tts_total_tokens"] = actual["tts_input_tokens"] + actual["tts_output_audio_tokens"]
    actual["tts_usd"] = sum(
        float(item.get("cost_usd") or 0) for item in (actual.get("tts_by_language") or {}).values()
    )
    actual["total_billable_tokens"] = int(actual.get("translation_total_tokens") or 0) + int(
        actual.get("tts_total_tokens") or 0
    )
    actual["total_usd"] = (
        float(actual.get("transcription_usd") or 0)
        + float(actual.get("translation_usd") or 0)
        + float(actual.get("tts_usd") or 0)
    )
    actual["cost_basis"] = "translation_and_tts_tokens_plus_transcription_minutes"


def _set_actual_transcription_cost(
    settings: Settings,
    job_id: str,
    duration_seconds: float,
) -> None:
    job = jobs.get_job(settings, job_id) or {}
    actual = job.get("actual_cost_json") or {}
    actual["transcription_minutes"] = max(0.0, duration_seconds) / 60
    actual["transcription_usd"] = cost.transcription_duration_cost(settings, duration_seconds)
    _recalculate_actual_totals(actual)
    jobs.update_job(settings, job_id, actual_cost_json=actual)


def _set_actual_tts_cost(settings: Settings, job_id: str, lang_code: str, usage: dict[str, Any]) -> None:
    job = jobs.get_job(settings, job_id) or {}
    actual = job.get("actual_cost_json") or {}
    by_language = actual.setdefault("tts_by_language", {})
    by_language[lang_code] = {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_audio_tokens": int(usage.get("output_audio_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "cost_usd": float(usage.get("cost_usd") or 0),
    }
    _recalculate_actual_totals(actual)
    jobs.update_job(settings, job_id, actual_cost_json=actual)


def run_job(settings: Settings, job_id: str) -> None:
    path = job_dir(settings, job_id)
    log_path = logs_dir(settings, job_id) / "job.log"
    selected_languages: list[str] = []
    try:
        job = jobs.get_job(settings, job_id)
        if not job:
            return
        check_cancelled(settings, job_id)
        selected_languages = [code for code in job["selected_languages"] if code in LANGUAGES]
        config = job.get("config_json") or {}
        voice = config.get("voice") or settings.openai_tts_voice
        output_format = config.get("output_format") or settings.output_audio_format
        voice_style = config.get("voice_style") or settings.tts_naturalness_style

        jobs.set_running(settings, job_id, "بدء المعالجة")
        log_event(
            settings,
            "job_started",
            "Background job started.",
            job_id=job_id,
            job_path=path,
            input_type=job.get("input_type"),
            language_count=len(selected_languages),
            voice=voice,
            output_format=output_format,
        )
        check_cancelled(settings, job_id)

        jobs.set_progress(settings, job_id, "تجهيز المصدر", 3)
        check_cancelled(settings, job_id)
        source_path = _load_source(settings, job, path, log_path)
        check_cancelled(settings, job_id)
        media.ffprobe_json(source_path)
        log_event(
            settings,
            "source_ready",
            "Source file is ready.",
            job_id=job_id,
            job_path=path,
            source_file=source_path.name,
            source_bytes=source_path.stat().st_size,
        )
        _checkpoint(path, "source_saved", {"path": str(source_path)})

        normalized_audio = path / "audio_normalized.mp3"
        if not normalized_audio.exists() or normalized_audio.stat().st_size == 0:
            jobs.set_progress(settings, job_id, "استخراج وضغط الصوت", 9)
            check_cancelled(settings, job_id)
            media.extract_or_normalize_audio(source_path, normalized_audio, settings, log_path)
            check_cancelled(settings, job_id)
        duration_seconds = media.probe_duration(normalized_audio)
        log_event(
            settings,
            "audio_normalized",
            "Audio extraction and normalization completed.",
            job_id=job_id,
            job_path=path,
            duration_seconds=duration_seconds,
            output_bytes=normalized_audio.stat().st_size,
        )
        write_json(path / "media.json", {"duration_seconds": duration_seconds, "audio_path": str(normalized_audio)})
        _checkpoint(path, "audio_extracted", {"duration_seconds": duration_seconds})

        early_estimate = cost.estimate_from_minutes(settings, duration_seconds / 60, len(selected_languages))
        jobs.update_job(settings, job_id, estimated_cost_json=early_estimate)
        recorded_usage = cost.recorded_jobs_summary(jobs.list_all_jobs(settings))
        budget = cost.budget_status(
            settings,
            early_estimate,
            None,
            recorded_cost_usd=recorded_usage["total_usd"],
        )
        log_event(
            settings,
            "post_download_budget_check",
            "Real media duration and budget were checked before API use.",
            job_id=job_id,
            job_path=path,
            duration_seconds=duration_seconds,
            estimated_total_usd=early_estimate.get("total_usd"),
            available_usd=budget.get("available_usd"),
            allowed=budget.get("allowed"),
            balance_source=budget.get("source"),
        )
        if not budget.get("allowed", True):
            raise BudgetBlocked(
                "الرصيد المحسوب أقل من التكلفة المتوقعة بعد قراءة مدة الملف. "
                "زد الرصيد المبدئي أو قلّل عدد اللغات، ثم استكمل العملية."
            )

        jobs.set_progress(settings, job_id, "تقسيم الصوت إلى أجزاء آمنة", 18)
        check_cancelled(settings, job_id)
        chunks = chunking.create_audio_chunks(normalized_audio, path, settings, log_path)
        check_cancelled(settings, job_id)
        log_event(
            settings,
            "chunks_created",
            "Audio chunks created.",
            job_id=job_id,
            job_path=path,
            chunk_count=len(chunks),
            largest_chunk_bytes=max((int(chunk.get("size_bytes") or 0) for chunk in chunks), default=0),
        )
        _checkpoint(path, "chunks_created", {"chunk_count": len(chunks)})

        segments = transcription.transcribe_chunks(
            settings,
            path,
            chunks,
            progress=_progress(settings, job_id, 22, 22),
            cancel_check=lambda: check_cancelled(settings, job_id),
        )
        check_cancelled(settings, job_id)
        log_event(
            settings,
            "transcription_completed",
            "Transcription completed.",
            job_id=job_id,
            job_path=path,
            segment_count=len(segments),
        )
        _set_actual_transcription_cost(settings, job_id, duration_seconds)
        _checkpoint(path, "transcription_completed", {"segment_count": len(segments)})

        refined_estimate = cost.estimate_from_segments(settings, segments, len(selected_languages))
        jobs.update_job(settings, job_id, estimated_cost_json=refined_estimate)

        language_count = max(1, len(selected_languages))
        for language_index, lang_code in enumerate(selected_languages):
            check_cancelled(settings, job_id)
            lang_meta = LANGUAGES[lang_code]
            target_language = lang_meta["name_en"]
            lang_base = 46 + (language_index / language_count) * 48
            lang_span = 48 / language_count

            translated_segments, usage = translation.translate_segments(
                settings,
                path,
                segments,
                lang_code,
                target_language,
                progress=_progress(settings, job_id, lang_base, lang_span * 0.35),
                cancel_check=lambda: check_cancelled(settings, job_id),
                usage_update=lambda current_usage, language=lang_code: _update_actual_translation_cost(
                    settings, job_id, language, current_usage
                ),
            )
            check_cancelled(settings, job_id)
            log_event(
                settings,
                "translation_completed",
                "Language translation completed.",
                job_id=job_id,
                job_path=path,
                language=lang_code,
                segment_count=len(translated_segments),
            )
            _update_actual_translation_cost(settings, job_id, lang_code, usage)
            _checkpoint(path, f"translation_completed_{lang_code}", {"usage": usage})
            _checkpoint(path, f"srt_completed_{lang_code}", {"path": str(path / f"translation_{lang_code}.srt")})

            tts.generate_voiceover(
                settings,
                path,
                translated_segments,
                lang_code,
                target_language,
                voice,
                output_format,
                duration_seconds,
                style=voice_style,
                progress=_progress(settings, job_id, lang_base + lang_span * 0.35, lang_span * 0.65),
                cancel_check=lambda: check_cancelled(settings, job_id),
                usage_update=lambda current_usage, language=lang_code: _set_actual_tts_cost(
                    settings, job_id, language, current_usage
                ),
            )
            check_cancelled(settings, job_id)
            tts_usage = read_json(path / f"tts_usage_{lang_code}.json", {})
            if isinstance(tts_usage, dict):
                _set_actual_tts_cost(settings, job_id, lang_code, tts_usage)
            log_event(
                settings,
                "voiceover_completed",
                "Language voiceover completed.",
                job_id=job_id,
                job_path=path,
                language=lang_code,
            )
            _checkpoint(path, f"tts_completed_{lang_code}", {"format": output_format})

            outputs = collect_language_outputs(settings, job_id, selected_languages)
            jobs.update_job(settings, job_id, output_paths=outputs)

        outputs = collect_language_outputs(settings, job_id, selected_languages)
        actual = (jobs.get_job(settings, job_id) or {}).get("actual_cost_json") or {}
        _recalculate_actual_totals(actual)
        _checkpoint(path, "final_files_ready", {"outputs": outputs})
        jobs.complete_job(settings, job_id, outputs, actual)
        log_event(
            settings,
            "job_completed",
            "Background job completed successfully.",
            job_id=job_id,
            job_path=path,
            output_languages=list(outputs),
        )
    except JobCancelled:
        outputs = collect_language_outputs(settings, job_id, selected_languages)
        if outputs:
            jobs.update_job(settings, job_id, output_paths=outputs)
        jobs.cancel_job(settings, job_id)
        log_event(
            settings,
            "job_cancelled",
            "Background job cancelled safely.",
            job_id=job_id,
            job_path=path,
        )
    except BudgetBlocked as exc:
        jobs.wait_for_budget(settings, job_id, str(exc))
        log_event(
            settings,
            "job_waiting_for_budget",
            str(exc),
            level="WARNING",
            job_id=job_id,
            job_path=path,
        )
    except youtube.YouTubeError as exc:
        log_event(
            settings,
            "youtube_source_failed",
            str(exc),
            level="WARNING" if exc.needs_local_audio else "ERROR",
            job_id=job_id,
            job_path=path,
            failure_type=exc.failure_type,
            recovery="upload_local_audio" if exc.needs_local_audio else "none",
        )
        if exc.needs_local_audio:
            jobs.wait_for_local_audio(settings, job_id, str(exc))
        else:
            jobs.fail_job(settings, job_id, str(exc))
    except Exception as exc:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(traceback.format_exc())
            log_file.write("\n")
        log_event(
            settings,
            "job_failed",
            str(exc),
            level="ERROR",
            job_id=job_id,
            job_path=path,
            exception_type=type(exc).__name__,
        )
        jobs.fail_job(settings, job_id, _arabic_error_message(exc, log_path))


def _arabic_error_message(exc: Exception, log_path: Path) -> str:
    if isinstance(exc, youtube.YouTubeError):
        return str(exc)
    raw = str(exc)
    lowered = raw.lower()
    if "insufficient_quota" in lowered or "quota" in lowered:
        return "رصيد OpenAI غير كاف أو الحصة منتهية. أضف رصيدًا ثم أعد المحاولة."
    if "api key" in lowered or "openai_api_key" in lowered:
        return "مفتاح OpenAI غير صحيح أو غير موجود."
    if "rate" in lowered:
        return "حدث ضغط مؤقت على واجهة OpenAI. تمت المحاولة عدة مرات ثم توقفت العملية."
    if "youtube" in lowered or "yt-dlp" in lowered:
        return "تعذر استخراج الصوت من YouTube. تأكد من الرابط وأن لديك صلاحية استخدامه."
    if "ffmpeg" in lowered or "ffprobe" in lowered:
        return "حدث خطأ في معالجة الصوت. تأكد من تثبيت ffmpeg. سجل الخطأ: " + str(log_path)
    return f"حدث خطأ أثناء المعالجة. سجل التفاصيل: {log_path}"
