# Implementation Checklist

Status for the current app in this folder.

- [x] Arabic RTL Streamlit dashboard with simple modern sections.
- [x] Upload input for local video/audio files.
- [x] YouTube URL input using `yt-dlp` audio-only extraction when possible.
- [x] Safe local storage under `data/jobs/{job_id}`.
- [x] ffmpeg/ffprobe extraction, normalization, mono audio, sample-rate and bitrate config.
- [x] Chunking with a default 22 MiB cap, below OpenAI's 25 MB upload limit.
- [x] Chunk metadata saved as `chunks/chunks.json`.
- [x] OpenAI transcription through local chunk files.
- [x] Timestamp-preserving transcript merge for `whisper-1` verbose segment output.
- [x] Fallback transcription path for newer JSON-only transcription models.
- [x] Source transcript saved as JSON, SRT, and TXT.
- [x] Translation/fixing prompt keeps segment IDs and validates strict JSON.
- [x] Translation repair retry if the model returns invalid JSON.
- [x] Prompt text configurable through `.env` or Streamlit secrets.
- [x] One SRT file per target language.
- [x] OpenAI TTS voiceover per target language.
- [x] Long TTS segments split into smaller TTS calls and stitched back.
- [x] Voiceover timeline assembled with silence based on original timestamps.
- [x] Controlled speed-up/trim when generated speech exceeds the time window.
- [x] Exactly two final downloadable files per language: SRT plus MP3/M4A.
- [x] SQLite job table with status, progress, costs, errors, outputs, and config.
- [x] One-active-job guard for personal single-user use.
- [x] Background worker thread keeps working across browser refresh/close while the Streamlit process remains alive.
- [x] Startup recovery marks unfinished jobs as interrupted and offers resume.
- [x] Cost estimates before starting and updated after media/transcription data exists.
- [x] Budget guard using manual balance or Admin Costs API spend when configured.
- [x] No dashboard scraping for balance.
- [x] `.env.example`, `requirements.txt`, `packages.txt`, Streamlit config, and README created.
- [x] Reusable professional Codex prompt added in `CODEX_PROMPT.md`.
- [x] Cancel requests propagate through source, chunk, transcription, translation, and TTS stages.
- [x] Finished/failed/cancelled job messages can be dismissed without deleting files.
- [x] Voice styles and cached voice preview samples are available in the Arabic UI.
- [x] Block-based TTS prevents single-speaker overlap and compresses normal silence gaps.
- [x] Voice QA reports are saved and downloadable per language.
- [x] Hindi, Malay, Indonesian, German, and Mandarin Chinese are available.
- [x] Structured logs redact configured API keys and YouTube diagnostics are downloadable.
- [x] Regression tests cover cancellation, languages, styles, preview text, block grouping, no-overlap scheduling, SRT, QA schema, logging redaction, and repository hygiene.

Known operational limits:

- Streamlit Community Cloud storage is ephemeral, so final outputs should be downloaded after each run.
- Very long videos may exceed free hosting CPU/RAM/time practicality even though the app streams to disk.
- `whisper-1` is the safest default when tight segment timestamps are required.
