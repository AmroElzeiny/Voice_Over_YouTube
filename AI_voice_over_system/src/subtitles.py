from __future__ import annotations

import re
import textwrap
from pathlib import Path


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole_seconds = int(seconds % 60)
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    if milliseconds >= 1000:
        whole_seconds += 1
        milliseconds -= 1000
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def wrap_subtitle_text(text: str, width: int = 44) -> str:
    text = clean_text(text)
    if not text:
        return ""
    return "\n".join(textwrap.wrap(text, width=width, break_long_words=False, replace_whitespace=False))


def write_srt(path: Path, segments: list[dict], text_field: str = "target_text") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_end = 0.0
    blocks: list[str] = []

    for index, segment in enumerate(segments, start=1):
        start = max(float(segment.get("start", 0.0)), previous_end)
        end = max(float(segment.get("end", start + 0.2)), start + 0.2)
        previous_end = end
        text = wrap_subtitle_text(str(segment.get(text_field) or segment.get("source_text") or ""))
        blocks.append(f"{index}\n{format_timestamp(start)} --> {format_timestamp(end)}\n{text}")

    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return path


def write_txt(path: Path, segments: list[dict], text_field: str = "source_text") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [clean_text(str(segment.get(text_field) or "")) for segment in segments]
    path.write_text("\n".join(line for line in lines if line) + "\n", encoding="utf-8")
    return path

