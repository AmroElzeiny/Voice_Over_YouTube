from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

from .config import Settings


AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".webm"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


class MediaError(RuntimeError):
    pass


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise MediaError("ffmpeg أو ffprobe غير مثبت. في Streamlit Cloud تأكد من وجود ffmpeg في packages.txt.")


def run_command(args: list[str], log_path: Path | None = None) -> None:
    ensure_ffmpeg()
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise MediaError("لم يتم العثور على ffmpeg أو الأداة المطلوبة.") from exc

    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write("$ " + " ".join(args) + "\n")
            if result.stdout:
                log_file.write(result.stdout + "\n")
            if result.stderr:
                log_file.write(result.stderr + "\n")

    if result.returncode != 0:
        raise MediaError(f"فشل أمر الوسائط. راجع ملف السجل: {log_path}" if log_path else "فشل أمر الوسائط.")


def ffprobe_json(path: Path) -> dict:
    ensure_ffmpeg()
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,bit_rate",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MediaError("تعذر قراءة مدة الملف باستخدام ffprobe.")
    return json.loads(result.stdout or "{}")


def probe_duration(path: Path) -> float:
    payload = ffprobe_json(path)
    duration = payload.get("format", {}).get("duration")
    try:
        return max(0.0, float(duration))
    except (TypeError, ValueError):
        return 0.0


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def normalize_audio(input_path: Path, output_path: Path, settings: Settings, log_path: Path | None = None) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        str(settings.audio_channels),
        "-ar",
        str(settings.audio_sample_rate),
        "-b:a",
        settings.audio_bitrate,
        "-map_metadata",
        "-1",
        str(output_path),
    ]
    run_command(args, log_path)
    return output_path


def extract_or_normalize_audio(source_path: Path, output_path: Path, settings: Settings, log_path: Path | None = None) -> Path:
    if not source_path.exists():
        raise MediaError("ملف المصدر غير موجود.")
    return normalize_audio(source_path, output_path, settings, log_path)


def speed_up_audio(input_path: Path, output_path: Path, factor: float, log_path: Path | None = None) -> Path:
    if factor <= 1.01:
        shutil.copyfile(input_path, output_path)
        return output_path
    factor = min(max(factor, 1.01), 2.0)
    args = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-filter:a",
        f"atempo={factor:.3f}",
        "-vn",
        str(output_path),
    ]
    run_command(args, log_path)
    return output_path


def bitrate_to_bits_per_second(value: str) -> int:
    raw = value.strip().lower()
    if raw.endswith("k"):
        return int(float(raw[:-1]) * 1000)
    if raw.endswith("m"):
        return int(float(raw[:-1]) * 1_000_000)
    try:
        return int(raw)
    except ValueError:
        return 64_000


def safe_chunk_duration_seconds(settings: Settings) -> int:
    max_bytes = settings.max_chunk_mb * 1024 * 1024
    bitrate = bitrate_to_bits_per_second(settings.audio_bitrate)
    seconds = math.floor((max_bytes * 8 / bitrate) * 0.88)
    return max(30, min(seconds, 20 * 60))

