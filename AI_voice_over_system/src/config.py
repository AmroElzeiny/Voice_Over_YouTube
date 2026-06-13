from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


BASE_DIR = Path(__file__).resolve().parents[1]

DEFAULT_TRANSCRIPTION_PROMPT = (
    "This is a YouTube/video transcript. Preserve technical terms, names, numbers, "
    "and speaker meaning as accurately as possible."
)

DEFAULT_TRANSLATION_SYSTEM_PROMPT = "Return strict JSON only. Do not include markdown or explanations."

DEFAULT_TRANSLATION_PROMPT_TEMPLATE = """
You are a professional subtitle translator and voiceover script editor.

Task:
Translate the provided transcript segments into {target_language}. Make the result natural, clear, and suitable for YouTube subtitles and AI voiceover.

Rules:
- Preserve every segment id exactly.
- Do not merge, remove, or add segments.
- Keep the meaning accurate.
- Make the wording natural, simple, and spoken-friendly.
- Keep the translation speakable and natural for voiceover.
- Avoid expanding short source sentences into unnecessarily long target sentences.
- Prefer concise phrasing when the target language would otherwise become much longer.
- Do not make the result sound robotic.
- Avoid long subtitle lines when possible.
- Follow this language-specific instruction: {language_instruction}
- Do not include explanations.
- Do not include markdown.
- Return valid JSON only in the required schema.

Required JSON schema:
{{
  "segments": [
    {{"id": 1, "target_text": "translated text"}}
  ]
}}

Segments:
{segments_json}
""".strip()

DEFAULT_TRANSLATION_REPAIR_PROMPT_TEMPLATE = (
    "Return valid JSON containing every requested segment exactly once. "
    "Use only these segment IDs: {expected_ids}"
)

DEFAULT_TTS_INSTRUCTIONS_TEMPLATE = (
    "Speak in a natural, warm, human-like YouTube voiceover style in {target_language}. "
    "Use clear pronunciation, natural pacing, and smooth sentence flow. Do not sound robotic. "
    "Do not add extra words. Keep the delivery close to the provided text. {style_instruction}"
)


LANGUAGES: dict[str, dict[str, str]] = {
    "ar": {"label_ar": "العربية", "name_en": "Arabic"},
    "en": {"label_ar": "الإنجليزية", "name_en": "English"},
    "es": {"label_ar": "الإسبانية", "name_en": "Spanish"},
    "fr": {"label_ar": "الفرنسية", "name_en": "French"},
    "de": {"label_ar": "الألمانية", "name_en": "German"},
    "it": {"label_ar": "الإيطالية", "name_en": "Italian"},
    "tr": {"label_ar": "التركية", "name_en": "Turkish"},
    "ru": {"label_ar": "الروسية", "name_en": "Russian"},
    "nl": {"label_ar": "الهولندية", "name_en": "Dutch"},
    "pt": {"label_ar": "البرتغالية", "name_en": "Portuguese"},
    "hi": {"label_ar": "الهندية", "name_en": "Hindi"},
    "ms": {"label_ar": "الماليزية", "name_en": "Malay"},
    "id": {"label_ar": "الإندونيسية", "name_en": "Indonesian"},
    "zh": {"label_ar": "الصينية - الماندرين", "name_en": "Mandarin Chinese"},
}

TTS_STYLES: dict[str, dict[str, str]] = {
    "warm_neutral": {
        "label_ar": "طبيعي ودافئ",
        "instruction": "Use a warm, neutral, conversational delivery.",
    },
    "educational": {
        "label_ar": "تعليمي",
        "instruction": "Use a clear, patient educational delivery with confident explanations.",
    },
    "documentary": {
        "label_ar": "وثائقي",
        "instruction": "Use a polished documentary narration style with measured pacing.",
    },
    "energetic": {
        "label_ar": "حماسي",
        "instruction": "Use an engaging energetic delivery without rushing or exaggerating.",
    },
    "calm": {
        "label_ar": "هادئ",
        "instruction": "Use a calm, reassuring delivery with smooth pacing.",
    },
}

TTS_VOICES = [
    "coral",
    "alloy",
    "ash",
    "ballad",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
]


def _streamlit_secret(name: str) -> Any | None:
    try:
        import streamlit as st

        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        return None
    return None


def setting(name: str, default: str = "") -> str:
    value = _streamlit_secret(name)
    if value is None:
        value = os.getenv(name, default)
    if value is None:
        return default
    return str(value).strip()


def setting_bool(name: str, default: bool) -> bool:
    raw = setting(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "y", "on"}


def setting_int(name: str, default: int) -> int:
    raw = setting(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def setting_float(name: str, default: float) -> float:
    raw = setting(name, str(default))
    try:
        return float(raw)
    except ValueError:
        return default


def setting_float_first(names: list[str], default: float) -> float:
    for name in names:
        raw = setting(name, "")
        if raw:
            try:
                return float(raw)
            except ValueError:
                continue
    return default


def setting_csv(name: str, default: str) -> list[str]:
    raw = setting(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def resolve_project_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


@dataclass(frozen=True)
class Settings:
    base_dir: Path
    openai_api_key: str
    openai_admin_key: str
    openai_transcription_model: str
    openai_text_model: str
    openai_tts_model: str
    openai_tts_voice: str
    whisper_1_usd_per_min: float
    gpt_4o_mini_input_usd_per_1m: float
    gpt_4o_mini_output_usd_per_1m: float
    gpt_4o_mini_tts_text_input_usd_per_1m: float
    gpt_4o_mini_tts_audio_output_usd_per_1m: float
    openai_tts_estimated_usd_per_min: float
    openai_monthly_budget_usd: float | None
    openai_manual_available_balance_usd: float | None
    cost_safety_buffer_percent: float
    max_upload_mb: int
    max_chunk_mb: int
    audio_bitrate: str
    audio_sample_rate: int
    audio_channels: int
    output_audio_format: str
    jobs_dir: Path
    sqlite_path: Path
    allow_one_active_job_only: bool
    keep_intermediate_files: bool
    max_tts_segment_tokens: int
    max_tts_speedup: float
    allow_tts_overlap_seconds: float
    tts_block_max_chars: int
    tts_block_max_duration_seconds: float
    tts_max_internal_gap_seconds: float
    tts_max_silence_gap_seconds: float
    tts_crossfade_ms: int
    tts_regen_on_qa_fail: bool
    tts_qa_max_overlap_seconds: float
    tts_qa_max_long_silence_seconds: float
    tts_naturalness_style: str
    voice_samples_dir: Path
    app_log_path: Path
    log_level: str
    yt_dlp_js_runtime: str
    yt_dlp_cookies_file: Path | None
    yt_dlp_cookies_from_browser: str
    transcription_prompt: str
    translation_system_prompt: str
    translation_prompt_template: str
    translation_repair_prompt_template: str
    translation_batch_size: int
    translation_recovery_batch_size: int
    tts_instructions_template: str
    app_title: str
    default_target_languages: list[str]


def optional_float(name: str) -> float | None:
    raw = setting(name, "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def load_settings() -> Settings:
    output_format = setting("OUTPUT_AUDIO_FORMAT", "mp3").lower().lstrip(".")
    if output_format not in {"mp3", "m4a", "aac"}:
        output_format = "mp3"

    default_languages = [
        code for code in setting_csv("DEFAULT_TARGET_LANGUAGES", "ar,en") if code in LANGUAGES
    ] or ["ar", "en"]

    jobs_dir = resolve_project_path(setting("JOBS_DIR", "data/jobs"))
    sqlite_path = resolve_project_path(setting("SQLITE_PATH", "data/jobs/jobs.db"))
    voice_samples_dir = resolve_project_path(setting("VOICE_SAMPLES_DIR", "data/voice_samples"))
    app_log_path = resolve_project_path(setting("APP_LOG_PATH", "data/logs/app.log"))
    cookies_file_raw = setting("YT_DLP_COOKIES_FILE", "")
    cookies_file = resolve_project_path(cookies_file_raw) if cookies_file_raw else None
    naturalness_style = setting("TTS_NATURALNESS_STYLE", "warm_neutral")
    if naturalness_style not in TTS_STYLES:
        naturalness_style = "warm_neutral"

    return Settings(
        base_dir=BASE_DIR,
        openai_api_key=setting("OPENAI_API_KEY", ""),
        openai_admin_key=setting("OPENAI_ADMIN_KEY", ""),
        openai_transcription_model=setting("OPENAI_TRANSCRIPTION_MODEL", "whisper-1"),
        openai_text_model=setting("OPENAI_TEXT_MODEL", "gpt-4o-mini"),
        openai_tts_model=setting("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
        openai_tts_voice=setting("OPENAI_TTS_VOICE", "coral"),
        whisper_1_usd_per_min=setting_float_first(
            ["OPENAI_TRANSCRIPTION_USD_PER_MIN", "WHISPER_1_USD_PER_MIN"], 0.006
        ),
        gpt_4o_mini_input_usd_per_1m=setting_float_first(
            ["OPENAI_TEXT_INPUT_USD_PER_1M", "GPT_4O_MINI_INPUT_USD_PER_1M"], 0.15
        ),
        gpt_4o_mini_output_usd_per_1m=setting_float_first(
            ["OPENAI_TEXT_OUTPUT_USD_PER_1M", "GPT_4O_MINI_OUTPUT_USD_PER_1M"], 0.60
        ),
        gpt_4o_mini_tts_text_input_usd_per_1m=setting_float_first(
            ["OPENAI_TTS_TEXT_INPUT_USD_PER_1M", "GPT_4O_MINI_TTS_TEXT_INPUT_USD_PER_1M"], 0.60
        ),
        gpt_4o_mini_tts_audio_output_usd_per_1m=setting_float_first(
            ["OPENAI_TTS_AUDIO_OUTPUT_USD_PER_1M", "GPT_4O_MINI_TTS_AUDIO_OUTPUT_USD_PER_1M"],
            12.00,
        ),
        openai_tts_estimated_usd_per_min=setting_float("OPENAI_TTS_ESTIMATED_USD_PER_MIN", 0.015),
        openai_monthly_budget_usd=optional_float("OPENAI_MONTHLY_BUDGET_USD"),
        openai_manual_available_balance_usd=optional_float("OPENAI_MANUAL_AVAILABLE_BALANCE_USD"),
        cost_safety_buffer_percent=setting_float("COST_SAFETY_BUFFER_PERCENT", 15),
        max_upload_mb=setting_int("MAX_UPLOAD_MB", 400),
        max_chunk_mb=setting_int("MAX_CHUNK_MB", 22),
        audio_bitrate=setting("AUDIO_BITRATE", "64k"),
        audio_sample_rate=setting_int("AUDIO_SAMPLE_RATE", 24000),
        audio_channels=setting_int("AUDIO_CHANNELS", 1),
        output_audio_format=output_format,
        jobs_dir=jobs_dir,
        sqlite_path=sqlite_path,
        allow_one_active_job_only=setting_bool("ALLOW_ONE_ACTIVE_JOB_ONLY", True),
        keep_intermediate_files=setting_bool("KEEP_INTERMEDIATE_FILES", True),
        max_tts_segment_tokens=setting_int("MAX_TTS_SEGMENT_TOKENS", 1500),
        max_tts_speedup=setting_float("MAX_TTS_SPEEDUP", 1.20),
        allow_tts_overlap_seconds=setting_float("ALLOW_TTS_OVERLAP_SECONDS", 0.35),
        tts_block_max_chars=setting_int("TTS_BLOCK_MAX_CHARS", 600),
        tts_block_max_duration_seconds=setting_float("TTS_BLOCK_MAX_DURATION_SECONDS", 14),
        tts_max_internal_gap_seconds=setting_float("TTS_MAX_INTERNAL_GAP_SECONDS", 1.0),
        tts_max_silence_gap_seconds=setting_float("TTS_MAX_SILENCE_GAP_SECONDS", 0.9),
        tts_crossfade_ms=setting_int("TTS_CROSSFADE_MS", 60),
        tts_regen_on_qa_fail=setting_bool("TTS_REGEN_ON_QA_FAIL", True),
        tts_qa_max_overlap_seconds=setting_float("TTS_QA_MAX_OVERLAP_SECONDS", 0.05),
        tts_qa_max_long_silence_seconds=setting_float("TTS_QA_MAX_LONG_SILENCE_SECONDS", 1.25),
        tts_naturalness_style=naturalness_style,
        voice_samples_dir=voice_samples_dir,
        app_log_path=app_log_path,
        log_level=setting("LOG_LEVEL", "INFO").upper(),
        yt_dlp_js_runtime=setting("YT_DLP_JS_RUNTIME", "auto").lower(),
        yt_dlp_cookies_file=cookies_file,
        yt_dlp_cookies_from_browser=setting("YT_DLP_COOKIES_FROM_BROWSER", ""),
        transcription_prompt=setting("OPENAI_TRANSCRIPTION_PROMPT", DEFAULT_TRANSCRIPTION_PROMPT)
        or DEFAULT_TRANSCRIPTION_PROMPT,
        translation_system_prompt=setting("TRANSLATION_SYSTEM_PROMPT", DEFAULT_TRANSLATION_SYSTEM_PROMPT)
        or DEFAULT_TRANSLATION_SYSTEM_PROMPT,
        translation_prompt_template=setting("TRANSLATION_PROMPT_TEMPLATE", DEFAULT_TRANSLATION_PROMPT_TEMPLATE)
        or DEFAULT_TRANSLATION_PROMPT_TEMPLATE,
        translation_repair_prompt_template=setting(
            "TRANSLATION_REPAIR_PROMPT_TEMPLATE", DEFAULT_TRANSLATION_REPAIR_PROMPT_TEMPLATE
        )
        or DEFAULT_TRANSLATION_REPAIR_PROMPT_TEMPLATE,
        translation_batch_size=max(1, setting_int("TRANSLATION_BATCH_SIZE", 20)),
        translation_recovery_batch_size=max(1, setting_int("TRANSLATION_RECOVERY_BATCH_SIZE", 5)),
        tts_instructions_template=setting("TTS_INSTRUCTIONS_TEMPLATE", DEFAULT_TTS_INSTRUCTIONS_TEMPLATE)
        or DEFAULT_TTS_INSTRUCTIONS_TEMPLATE,
        app_title=setting("APP_TITLE", "مترجم الفيديو والتعليق الصوتي"),
        default_target_languages=default_languages,
    )
