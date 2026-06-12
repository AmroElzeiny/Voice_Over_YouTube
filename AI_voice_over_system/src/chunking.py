from __future__ import annotations

from pathlib import Path

from . import media
from .config import Settings
from .storage import read_json, write_json


def create_audio_chunks(
    audio_path: Path,
    job_path: Path,
    settings: Settings,
    log_path: Path | None = None,
) -> list[dict]:
    chunks_dir = job_path / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = chunks_dir / "chunks.json"
    existing = read_json(metadata_path, [])
    if existing and all((chunks_dir / item["filename"]).exists() for item in existing):
        return existing

    duration = media.probe_duration(audio_path)
    max_bytes = settings.max_chunk_mb * 1024 * 1024
    base_duration = media.safe_chunk_duration_seconds(settings)
    chunks: list[dict] = []
    cursor = 0.0
    index = 0

    while cursor < duration or (duration == 0 and index == 0):
        remaining = max(0.0, duration - cursor)
        segment_duration = min(base_duration, remaining) if remaining else base_duration
        segment_duration = max(1.0, segment_duration)
        output = chunks_dir / f"chunk_{index:03d}.mp3"

        while True:
            args = [
                "ffmpeg",
                "-y",
                "-ss",
                f"{cursor:.3f}",
                "-t",
                f"{segment_duration:.3f}",
                "-i",
                str(audio_path),
                "-vn",
                "-ac",
                str(settings.audio_channels),
                "-ar",
                str(settings.audio_sample_rate),
                "-b:a",
                settings.audio_bitrate,
                str(output),
            ]
            media.run_command(args, log_path)
            if output.stat().st_size <= max_bytes or segment_duration <= 10:
                break
            segment_duration *= 0.82

        actual_duration = media.probe_duration(output)
        size_bytes = output.stat().st_size
        chunks.append(
            {
                "filename": output.name,
                "offset_start_seconds": cursor,
                "duration_seconds": actual_duration,
                "size_bytes": size_bytes,
            }
        )

        cursor += segment_duration
        index += 1
        if duration == 0:
            break

    write_json(metadata_path, chunks)
    return chunks

