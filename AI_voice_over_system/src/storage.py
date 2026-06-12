from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, BinaryIO

from .config import Settings


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def ensure_storage(settings: Settings) -> None:
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    settings.voice_samples_dir.mkdir(parents=True, exist_ok=True)


def safe_filename(name: str, default: str = "source") -> str:
    cleaned = SAFE_NAME_RE.sub("_", Path(name or default).name).strip("._")
    return cleaned or default


def job_dir(settings: Settings, job_id: str) -> Path:
    path = settings.jobs_dir / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def source_dir(settings: Settings, job_id: str) -> Path:
    path = job_dir(settings, job_id) / "source"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir(settings: Settings, job_id: str) -> Path:
    path = job_dir(settings, job_id) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_uploaded_file(file_obj: BinaryIO, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as out_file:
        shutil.copyfileobj(file_obj, out_file, length=1024 * 1024)
    return destination


def find_first_file(path: Path, patterns: list[str]) -> Path | None:
    for pattern in patterns:
        matches = sorted(path.glob(pattern))
        if matches:
            return matches[0]
    return None


def collect_language_outputs(settings: Settings, job_id: str, languages: list[str]) -> dict[str, dict[str, str]]:
    base = job_dir(settings, job_id)
    outputs: dict[str, dict[str, str]] = {}
    for lang in languages:
        srt = base / f"translation_{lang}.srt"
        audio = find_first_file(base, [f"voiceover_{lang}.mp3", f"voiceover_{lang}.m4a", f"voiceover_{lang}.aac"])
        qa = base / f"voiceover_{lang}_qa.json"
        lang_outputs: dict[str, str] = {}
        if srt.exists():
            lang_outputs["srt"] = str(srt)
        if audio and audio.exists():
            lang_outputs["audio"] = str(audio)
        if qa.exists():
            lang_outputs["qa"] = str(qa)
        if lang_outputs:
            outputs[lang] = lang_outputs
    return outputs
