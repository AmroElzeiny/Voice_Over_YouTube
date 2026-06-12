from __future__ import annotations

from pathlib import Path
from typing import Callable

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from . import subtitles
from .config import Settings
from .openai_client import get_client, response_to_dict
from .storage import read_json, write_json

ProgressCallback = Callable[[str, float], None]
CancelCallback = Callable[[], None]


def _supports_verbose_segments(model: str) -> bool:
    return model.strip().lower() == "whisper-1"


def _is_retryable(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return any(token in name or token in message for token in ("rate", "timeout", "connection", "temporar", "server"))


def _is_parameter_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "unsupported parameter",
            "unknown parameter",
            "invalid parameter",
            "response_format",
            "timestamp_granularities",
            "prompt",
        )
    )


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _transcribe_file(client, model: str, audio_path: Path, prompt: str):
    with audio_path.open("rb") as audio_file:
        if _supports_verbose_segments(model):
            try:
                return client.audio.transcriptions.create(
                    model=model,
                    file=audio_file,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                    temperature=0,
                    prompt=prompt,
                )
            except (TypeError, Exception) as exc:
                if not isinstance(exc, TypeError) and not _is_parameter_error(exc):
                    raise
                audio_file.seek(0)
                return client.audio.transcriptions.create(
                    model=model,
                    file=audio_file,
                    response_format="verbose_json",
                    temperature=0,
                    prompt=prompt,
                )

        try:
            return client.audio.transcriptions.create(
                model=model,
                file=audio_file,
                response_format="json",
                prompt=prompt,
            )
        except (TypeError, Exception) as exc:
            if not isinstance(exc, TypeError) and not _is_parameter_error(exc):
                raise
            audio_file.seek(0)
            return client.audio.transcriptions.create(
                model=model,
                file=audio_file,
                response_format="json",
            )


def transcribe_chunks(
    settings: Settings,
    job_path: Path,
    chunks: list[dict],
    progress: ProgressCallback | None = None,
    cancel_check: CancelCallback | None = None,
) -> list[dict]:
    transcript_path = job_path / "transcript_source.json"
    existing = read_json(transcript_path)
    if isinstance(existing, dict) and existing.get("segments"):
        return existing["segments"]

    client = get_client(settings)
    chunks_dir = job_path / "chunks"
    merged: list[dict] = []
    next_id = 1
    total = max(1, len(chunks))

    for index, chunk in enumerate(chunks):
        if cancel_check:
            cancel_check()
        chunk_path = chunks_dir / chunk["filename"]
        if progress:
            progress(f"تفريغ المقطع الصوتي {index + 1} من {total}", index / total)
        response = _transcribe_file(
            client,
            settings.openai_transcription_model,
            chunk_path,
            settings.transcription_prompt,
        )
        if cancel_check:
            cancel_check()
        payload = response_to_dict(response)
        offset = float(chunk.get("offset_start_seconds", 0.0))
        chunk_segments = payload.get("segments") or []

        if not chunk_segments and payload.get("text"):
            chunk_segments = [
                {
                    "start": 0.0,
                    "end": float(chunk.get("duration_seconds", 0.0)),
                    "text": payload.get("text", ""),
                }
            ]

        for item in chunk_segments:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            start = offset + float(item.get("start") or 0.0)
            end = offset + float(item.get("end") or start + 0.2)
            if end <= start:
                end = start + 0.2
            merged.append(
                {
                    "id": next_id,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "source_text": text,
                    "target_text": None,
                }
            )
            next_id += 1

    write_json(
        transcript_path,
        {
            "model": settings.openai_transcription_model,
            "chunk_count": len(chunks),
            "segments": merged,
        },
    )
    subtitles.write_srt(job_path / "transcript_source.srt", merged, text_field="source_text")
    subtitles.write_txt(job_path / "transcript_source.txt", merged, text_field="source_text")
    if progress:
        progress("اكتمل التفريغ النصي", 1.0)
    return merged
