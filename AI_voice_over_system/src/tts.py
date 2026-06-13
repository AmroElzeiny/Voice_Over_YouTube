from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable

from pydub import AudioSegment
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from . import cost, media
from .config import TTS_STYLES, Settings
from .openai_client import get_client
from .storage import read_json, write_json

ProgressCallback = Callable[[str, float], None]
CancelCallback = Callable[[], None]
UsageCallback = Callable[[dict], None]

VOICE_SAMPLE_TEXT = "Hello, this is me. How may I help you?"


def _split_text_for_tts(text: str, max_tokens: int) -> list[str]:
    """Split unusually long text using a conservative character/token estimate."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    max_chars = max(350, int(max_tokens * 3.6))
    if len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?؟؛;:])\s+", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        for word in sentence.split():
            candidate = f"{current} {word}".strip()
            if len(candidate) > max_chars and current:
                parts.append(current)
                current = word
            else:
                current = candidate
    if current:
        parts.append(current)
    return parts


def prepare_tts_text(text: str, target_language: str) -> str:
    """Make subtitle text flow naturally without changing its meaning."""
    prepared = re.sub(r"[\r\n]+", " ", text or "")
    prepared = re.sub(r"\s+", " ", prepared).strip()
    prepared = re.sub(r"\s*[,،]{2,}\s*", ", ", prepared)
    prepared = re.sub(r"\s*([.!?؟؛;])\1+", r"\1", prepared)
    prepared = re.sub(r"\s+([,.!?؟؛;:])", r"\1", prepared)
    if prepared and prepared[-1] not in ".!?؟؛;。！？":
        prepared += "。" if "Chinese" in target_language else "."
    return prepared


def _join_block_text(current: str, new_text: str) -> str:
    current = current.strip()
    new_text = new_text.strip()
    if not current:
        return new_text
    if not new_text:
        return current
    separator = " " if current[-1] in ".!?؟؛;:。！？" else ". "
    return current + separator + new_text


def build_tts_blocks(segments: list[dict], settings: Settings) -> list[dict]:
    """Group adjacent subtitle segments into longer, more natural speech blocks."""
    blocks: list[dict] = []
    current: dict | None = None

    for segment in segments:
        segment_id = int(segment["id"])
        start = float(segment.get("start") or 0.0)
        end = max(start + 0.2, float(segment.get("end") or start + 0.2))
        text = str(segment.get("target_text") or "").strip()

        if current is None:
            current = {
                "block_id": len(blocks) + 1,
                "segment_ids": [segment_id],
                "start": start,
                "end": end,
                "text": text,
            }
            continue

        candidate_text = _join_block_text(str(current["text"]), text)
        gap = max(0.0, start - float(current["end"]))
        candidate_duration = end - float(current["start"])
        can_merge = (
            gap <= settings.tts_max_internal_gap_seconds
            and len(candidate_text) <= settings.tts_block_max_chars
            and candidate_duration <= settings.tts_block_max_duration_seconds
        )
        if can_merge:
            current["segment_ids"].append(segment_id)
            current["end"] = end
            current["text"] = candidate_text
        else:
            current["target_duration_ms"] = max(
                200, int((float(current["end"]) - float(current["start"])) * 1000)
            )
            blocks.append(current)
            current = {
                "block_id": len(blocks) + 1,
                "segment_ids": [segment_id],
                "start": start,
                "end": end,
                "text": text,
            }

    if current is not None:
        current["target_duration_ms"] = max(
            200, int((float(current["end"]) - float(current["start"])) * 1000)
        )
        blocks.append(current)
    return blocks


def schedule_tts_blocks(
    blocks: list[dict],
    audio_durations_ms: list[int],
    settings: Settings,
) -> list[dict]:
    """Place blocks sequentially, avoiding self-overlap and compressing normal gaps."""
    placements: list[dict] = []
    previous_audio_end_ms = 0
    previous_original_end_ms = 0
    max_gap_ms = int(settings.tts_max_silence_gap_seconds * 1000)
    continuous_gap_ms = int(max(3.0, settings.tts_max_internal_gap_seconds * 3) * 1000)

    for index, block in enumerate(blocks):
        preferred_start_ms = max(0, int(float(block["start"]) * 1000))
        duration_ms = max(0, int(audio_durations_ms[index]))
        original_start_ms = preferred_start_ms
        original_gap_ms = max(0, original_start_ms - previous_original_end_ms)
        compressed_gap_ms = 0

        if index == 0:
            start_ms = preferred_start_ms
        elif preferred_start_ms < previous_audio_end_ms:
            start_ms = previous_audio_end_ms
        else:
            gap_ms = preferred_start_ms - previous_audio_end_ms
            likely_continuous = original_gap_ms <= continuous_gap_ms
            if likely_continuous and gap_ms > max_gap_ms:
                start_ms = previous_audio_end_ms + max_gap_ms
                compressed_gap_ms = gap_ms - max_gap_ms
            else:
                start_ms = preferred_start_ms

        end_ms = start_ms + duration_ms
        placements.append(
            {
                "block_id": block["block_id"],
                "segment_ids": block["segment_ids"],
                "preferred_start_ms": preferred_start_ms,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "audio_duration_ms": duration_ms,
                "target_duration_ms": int(block["target_duration_ms"]),
                "compressed_gap_ms": compressed_gap_ms,
                "shift_ms": start_ms - preferred_start_ms,
            }
        )
        previous_audio_end_ms = end_ms
        previous_original_end_ms = int(float(block["end"]) * 1000)
    return placements


def analyze_voiceover_timeline(
    blocks: list[dict],
    placements: list[dict],
    final_audio: AudioSegment,
    settings: Settings,
) -> dict:
    """Return a serializable QA report for overlap, silence, duration, and fitting."""
    overlap_values: list[float] = []
    silence_values: list[float] = []
    previous_end_ms: int | None = None
    for placement in placements:
        if previous_end_ms is not None:
            overlap_ms = max(0, previous_end_ms - int(placement["start_ms"]))
            silence_ms = max(0, int(placement["start_ms"]) - previous_end_ms)
            if overlap_ms:
                overlap_values.append(overlap_ms / 1000)
            if silence_ms > settings.tts_qa_max_long_silence_seconds * 1000:
                silence_values.append(silence_ms / 1000)
        previous_end_ms = int(placement["end_ms"])

    original_duration = max((float(block["end"]) for block in blocks), default=0.0)
    final_duration = len(final_audio) / 1000
    trimmed_count = sum(1 for placement in placements if placement.get("trimmed"))
    regenerated_count = sum(1 for placement in placements if placement.get("regenerated"))
    empty_count = sum(1 for block in blocks if not str(block.get("text") or "").strip())
    warnings: list[str] = []
    max_overlap = max(overlap_values, default=0.0)
    max_silence = max(silence_values, default=0.0)
    if max_overlap > settings.tts_qa_max_overlap_seconds:
        warnings.append("Detected overlapping speech blocks.")
    if silence_values:
        warnings.append("Detected one or more long silence gaps.")
    if trimmed_count:
        warnings.append("One or more speech blocks were trimmed.")
    return {
        "total_duration_seconds": round(final_duration, 3),
        "original_duration_seconds": round(original_duration, 3),
        "duration_delta_seconds": round(final_duration - original_duration, 3),
        "overlap_count": len(overlap_values),
        "max_overlap_seconds": round(max_overlap, 3),
        "long_silence_count": len(silence_values),
        "max_silence_seconds": round(max_silence, 3),
        "trimmed_block_count": trimmed_count,
        "regenerated_block_count": regenerated_count,
        "empty_text_blocks": empty_count,
        "warnings": warnings,
        "passed": not warnings,
        "placements": placements,
    }


def _is_retryable(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return any(token in name or token in message for token in ("rate", "timeout", "connection", "temporar", "server"))


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _speech_create(client, model: str, voice: str, text: str, instructions: str, output_path: Path) -> None:
    try:
        response = client.audio.speech.create(
            model=model,
            voice=voice,
            input=text,
            instructions=instructions,
            response_format="mp3",
        )
    except TypeError:
        response = client.audio.speech.create(
            model=model,
            voice=voice,
            input=text,
            response_format="mp3",
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(response, "stream_to_file"):
        response.stream_to_file(str(output_path))
    elif hasattr(response, "content"):
        output_path.write_bytes(response.content)
    else:
        output_path.write_bytes(bytes(response))


def generate_voice_sample(settings: Settings, voice: str, sample_text: str, output_dir: Path) -> Path:
    """Generate and cache one short voice preview sample."""
    output_path = voice_sample_path(settings, voice, output_dir)
    instructions = (
        "Speak naturally in a warm, clear, human-like voice. Keep this preview short and friendly."
    )
    if not output_path.exists() or output_path.stat().st_size == 0:
        client = get_client(settings)
        _speech_create(client, settings.openai_tts_model, voice, sample_text, instructions, output_path)

    usage_path = voice_samples_usage_path(output_dir)
    usage = read_json(
        usage_path,
        {"model": settings.openai_tts_model, "parts": {}},
    )
    parts = usage.setdefault("parts", {})
    if output_path.name not in parts:
        audio = AudioSegment.from_file(output_path)
        input_tokens = cost.estimate_tokens(
            f"{instructions}\n{sample_text}", settings.openai_tts_model
        )
        output_audio_tokens = int(
            round(len(audio) / 1000 * settings.openai_tts_audio_tokens_per_second)
        )
        parts[output_path.name] = {
            "input_tokens": input_tokens,
            "output_audio_tokens": output_audio_tokens,
            "audio_seconds": round(len(audio) / 1000, 3),
        }
        usage["input_tokens"] = sum(int(item.get("input_tokens") or 0) for item in parts.values())
        usage["output_audio_tokens"] = sum(
            int(item.get("output_audio_tokens") or 0) for item in parts.values()
        )
        usage["total_tokens"] = int(usage["input_tokens"]) + int(usage["output_audio_tokens"])
        usage["cost_usd"] = cost.tts_token_usage_cost(
            settings,
            int(usage["input_tokens"]),
            int(usage["output_audio_tokens"]),
        )
        write_json(usage_path, usage)
    return output_path


def voice_sample_path(settings: Settings, voice: str, output_dir: Path) -> Path:
    """Return the deterministic cache path for a voice preview."""
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "_", settings.openai_tts_model)
    safe_voice = re.sub(r"[^A-Za-z0-9._-]+", "_", voice)
    return output_dir / f"{safe_model}_{safe_voice}.mp3"


def voice_samples_usage_path(output_dir: Path) -> Path:
    return output_dir / "usage.json"


def _render_instructions(settings: Settings, target_language: str, style: str, stricter: bool = False) -> str:
    style_meta = TTS_STYLES.get(style) or TTS_STYLES["warm_neutral"]
    values = {
        "target_language": target_language,
        "style": style,
        "style_instruction": style_meta["instruction"],
    }
    try:
        rendered = settings.tts_instructions_template.format(**values)
    except Exception:
        rendered = DEFAULT_INSTRUCTIONS.format(**values)
    if style_meta["instruction"] not in rendered:
        rendered += " " + style_meta["instruction"]
    rendered += (
        " Use natural sentence flow, avoid choppy subtitle-by-subtitle delivery, and do not sound robotic."
    )
    if stricter:
        rendered += (
            " Speak slightly faster and avoid unnecessary pauses, while keeping every word and all meaning. "
            "Do not add words."
        )
    return rendered


DEFAULT_INSTRUCTIONS = (
    "Speak in a natural, warm, human-like YouTube voiceover style in {target_language}. "
    "Use clear pronunciation, natural pacing, and smooth sentence flow. Do not sound robotic. "
    "Do not add extra words. Keep the delivery close to the provided text. {style_instruction}"
)


def _render_block_audio(
    client,
    settings: Settings,
    voice: str,
    text: str,
    instructions: str,
    block_id: int,
    tts_dir: Path,
    variant: str,
    usage_path: Path,
    usage_manifest: dict,
    usage_update: UsageCallback | None,
    cancel_check: CancelCallback | None,
) -> AudioSegment:
    text_parts = _split_text_for_tts(text, settings.max_tts_segment_tokens)
    if not text_parts:
        return AudioSegment.silent(duration=0, frame_rate=settings.audio_sample_rate)

    combined = AudioSegment.empty()
    for part_index, part_text in enumerate(text_parts):
        if cancel_check:
            cancel_check()
        part_path = tts_dir / f"block_{block_id:04d}_{variant}_{part_index:02d}.mp3"
        if not part_path.exists() or part_path.stat().st_size == 0:
            _speech_create(client, settings.openai_tts_model, voice, part_text, instructions, part_path)
        if cancel_check:
            cancel_check()
        part_audio = AudioSegment.from_file(part_path)
        part_key = part_path.name
        parts = usage_manifest.setdefault("parts", {})
        if part_key not in parts:
            input_tokens = cost.estimate_tokens(f"{instructions}\n{part_text}", settings.openai_tts_model)
            output_audio_tokens = int(
                round(len(part_audio) / 1000 * settings.openai_tts_audio_tokens_per_second)
            )
            parts[part_key] = {
                "input_tokens": input_tokens,
                "output_audio_tokens": output_audio_tokens,
                "audio_seconds": round(len(part_audio) / 1000, 3),
            }
            usage_manifest["input_tokens"] = sum(int(item.get("input_tokens") or 0) for item in parts.values())
            usage_manifest["output_audio_tokens"] = sum(
                int(item.get("output_audio_tokens") or 0) for item in parts.values()
            )
            usage_manifest["total_tokens"] = (
                int(usage_manifest["input_tokens"]) + int(usage_manifest["output_audio_tokens"])
            )
            usage_manifest["cost_usd"] = cost.tts_token_usage_cost(
                settings,
                int(usage_manifest["input_tokens"]),
                int(usage_manifest["output_audio_tokens"]),
            )
            write_json(usage_path, usage_manifest)
            if usage_update:
                usage_update(dict(usage_manifest))
        if len(combined) == 0:
            combined = part_audio
        else:
            crossfade_ms = min(
                settings.tts_crossfade_ms,
                max(0, len(combined) // 5),
                max(0, len(part_audio) // 5),
            )
            combined = combined.append(part_audio, crossfade=crossfade_ms)
    return combined


def _fit_block_audio(
    client,
    settings: Settings,
    voice: str,
    text: str,
    target_language: str,
    style: str,
    block: dict,
    tts_dir: Path,
    log_path: Path,
    usage_path: Path,
    usage_manifest: dict,
    usage_update: UsageCallback | None,
    cancel_check: CancelCallback | None,
) -> tuple[AudioSegment, dict]:
    normal_instructions = _render_instructions(settings, target_language, style)
    audio = _render_block_audio(
        client,
        settings,
        voice,
        text,
        normal_instructions,
        int(block["block_id"]),
        tts_dir,
        "normal",
        usage_path,
        usage_manifest,
        usage_update,
        cancel_check,
    )
    target_ms = int(block["target_duration_ms"])
    slack_ms = int(settings.tts_qa_max_overlap_seconds * 1000)
    regenerated = False
    speed_factor = 1.0

    if len(audio) > target_ms + slack_ms and settings.tts_regen_on_qa_fail:
        strict_audio = _render_block_audio(
            client,
            settings,
            voice,
            text,
            _render_instructions(settings, target_language, style, stricter=True),
            int(block["block_id"]),
            tts_dir,
            "regen",
            usage_path,
            usage_manifest,
            usage_update,
            cancel_check,
        )
        regenerated = True
        if 0 < len(strict_audio) < len(audio):
            audio = strict_audio

    if len(audio) > target_ms + slack_ms and target_ms > 0:
        speed_factor = min(settings.max_tts_speedup, len(audio) / target_ms)
        if speed_factor > 1.01:
            raw_path = tts_dir / f"block_{int(block['block_id']):04d}_fit_source.mp3"
            fast_path = tts_dir / f"block_{int(block['block_id']):04d}_fast.mp3"
            audio.export(raw_path, format="mp3", bitrate=settings.audio_bitrate)
            media.speed_up_audio(raw_path, fast_path, speed_factor, log_path)
            audio = AudioSegment.from_file(fast_path)

    fade_ms = min(settings.tts_crossfade_ms, max(0, len(audio) // 5))
    if fade_ms:
        audio = audio.fade_in(fade_ms).fade_out(fade_ms)
    return audio, {
        "regenerated": regenerated,
        "speed_factor": round(speed_factor, 3),
        "trimmed": False,
    }


def generate_voiceover(
    settings: Settings,
    job_path: Path,
    segments: list[dict],
    lang_code: str,
    target_language: str,
    voice: str,
    output_format: str,
    total_duration_seconds: float,
    style: str | None = None,
    progress: ProgressCallback | None = None,
    cancel_check: CancelCallback | None = None,
    usage_update: UsageCallback | None = None,
) -> Path:
    """Generate block-based speech and place it sequentially on a synced timeline."""
    output_format = output_format.lower().lstrip(".")
    if output_format == "aac":
        output_format = "m4a"
    if output_format not in {"mp3", "m4a"}:
        output_format = "mp3"

    final_path = job_path / f"voiceover_{lang_code}.{output_format}"
    qa_path = job_path / f"voiceover_{lang_code}_qa.json"
    if final_path.exists() and final_path.stat().st_size > 0 and qa_path.exists():
        existing_usage = read_json(job_path / f"tts_usage_{lang_code}.json", {})
        if usage_update and isinstance(existing_usage, dict):
            usage_update(existing_usage)
        return final_path

    media.ensure_ffmpeg()
    client = get_client(settings)
    selected_style = style if style in TTS_STYLES else settings.tts_naturalness_style
    tts_dir = job_path / "tts" / lang_code
    tts_dir.mkdir(parents=True, exist_ok=True)
    usage_path = job_path / f"tts_usage_{lang_code}.json"
    usage_manifest = read_json(
        usage_path,
        {
            "model": settings.openai_tts_model,
            "language": lang_code,
            "parts": {},
            "input_tokens": 0,
            "output_audio_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        },
    )
    if not isinstance(usage_manifest, dict) or usage_manifest.get("model") != settings.openai_tts_model:
        usage_manifest = {
            "model": settings.openai_tts_model,
            "language": lang_code,
            "parts": {},
            "input_tokens": 0,
            "output_audio_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        }
    log_path = job_path / "logs" / f"tts_{lang_code}.log"
    blocks = build_tts_blocks(segments, settings)
    block_audio: list[AudioSegment] = []
    fit_metadata: list[dict] = []
    total_blocks = max(1, len(blocks))

    for index, block in enumerate(blocks):
        if cancel_check:
            cancel_check()
        if progress:
            progress(
                f"إنشاء الصوت {target_language}: مقطع {index + 1} من {total_blocks}",
                index / total_blocks,
            )
        prepared_text = prepare_tts_text(str(block.get("text") or ""), target_language)
        block["prepared_text"] = prepared_text
        if not prepared_text:
            audio = AudioSegment.silent(duration=0, frame_rate=settings.audio_sample_rate)
            metadata = {"regenerated": False, "speed_factor": 1.0, "trimmed": False}
        else:
            audio, metadata = _fit_block_audio(
                client,
                settings,
                voice,
                prepared_text,
                target_language,
                selected_style,
                block,
                tts_dir,
                log_path,
                usage_path,
                usage_manifest,
                usage_update,
                cancel_check,
            )
        block_audio.append(audio)
        fit_metadata.append(metadata)

    placements = schedule_tts_blocks(blocks, [len(audio) for audio in block_audio], settings)
    for placement, metadata in zip(placements, fit_metadata):
        placement.update(metadata)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        for placement in placements:
            if placement["compressed_gap_ms"] > 0:
                log_file.write(
                    f"Compressed gap before block {placement['block_id']} by "
                    f"{placement['compressed_gap_ms'] / 1000:.3f}s.\n"
                )
            if placement["shift_ms"] > 0:
                log_file.write(
                    f"Shifted block {placement['block_id']} by {placement['shift_ms'] / 1000:.3f}s "
                    "to prevent self-overlap.\n"
                )

    last_end_ms = max((int(item["end_ms"]) for item in placements), default=0)
    original_duration_ms = max(1000, int(total_duration_seconds * 1000))
    timeline = AudioSegment.silent(
        duration=max(original_duration_ms, last_end_ms),
        frame_rate=settings.audio_sample_rate,
    )
    for placement, audio in zip(placements, block_audio):
        if cancel_check:
            cancel_check()
        timeline = timeline.overlay(audio, position=int(placement["start_ms"]))

    report = analyze_voiceover_timeline(blocks, placements, timeline, settings)
    report["original_duration_seconds"] = round(total_duration_seconds, 3)
    report["duration_delta_seconds"] = round(len(timeline) / 1000 - total_duration_seconds, 3)
    report["language"] = lang_code
    report["voice"] = voice
    report["style"] = selected_style
    report["block_count"] = len(blocks)
    if report["duration_delta_seconds"] > 2.0 and "Voiceover duration exceeds the original video." not in report["warnings"]:
        report["warnings"].append("Voiceover duration exceeds the original video.")
        report["passed"] = False
    write_json(qa_path, report)

    if output_format == "mp3":
        timeline.export(final_path, format="mp3", bitrate=settings.audio_bitrate)
    else:
        temp_aac = final_path.with_suffix(".aac")
        timeline.export(temp_aac, format="adts", bitrate=settings.audio_bitrate)
        media.run_command(
            ["ffmpeg", "-y", "-i", str(temp_aac), "-c:a", "aac", "-b:a", settings.audio_bitrate, str(final_path)],
            log_path,
        )
        if temp_aac.exists():
            temp_aac.unlink()

    if progress:
        progress(f"اكتمل صوت {target_language}", 1.0)
    if not settings.keep_intermediate_files:
        shutil.rmtree(tts_dir, ignore_errors=True)
    return final_path
