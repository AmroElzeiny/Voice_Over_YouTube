# Professional Codex Prompt

Use this prompt when asking Codex to rebuild, review, or extend this project without losing the intended context.

```text
You are Codex, acting as a senior Python/Streamlit engineer. Work inside the folder `AI_voice_over_system`.

Build and maintain a production-clean personal Streamlit Cloud app named "Arabic YouTube / Video Voiceover Translator".

Core objective:
Create an Arabic RTL dashboard where one personal user can either upload a local video/audio file or paste a YouTube URL. The app must extract/download audio only, compress it, split it into chunks under OpenAI's 25 MB audio upload limit, transcribe chunks through the OpenAI API, merge timestamps, translate/fix the transcript into one or more target languages, generate synced OpenAI TTS voiceover audio per language, and provide exactly two final download files per language:

1. `translation_{lang}.srt`
2. `voiceover_{lang}.mp3` or `voiceover_{lang}.m4a`

Non-negotiable architecture:
- Use Streamlit for the dashboard.
- Use Arabic UI wording, RTL layout, and Arabic help/hover text.
- Use `yt-dlp` for YouTube audio-only extraction whenever possible.
- Use `ffmpeg` and `ffprobe` through safe subprocess list arguments.
- Never pass user input into shell strings.
- Never store or print API keys.
- Prefer `st.secrets`, then environment variables, then `.env`.
- Use local disk job folders under `data/jobs/{job_id}`.
- Use SQLite at `data/jobs/jobs.db` as the source of truth for job state.
- Do not rely only on `st.session_state`.
- Use a background worker so refresh/browser close does not stop work while the Streamlit process remains alive.
- Enforce one active job at a time.
- On Streamlit process restart, mark running/queued jobs as interrupted and offer resume from checkpoint files.

Pipeline checklist:
- Validate source input.
- Save uploaded files to the job folder.
- For YouTube, use `yt-dlp --extract-audio`; do not download the full video unless absolutely required.
- Normalize audio to mono, low bitrate MP3, configurable sample rate and bitrate.
- Split audio into chunks targeting 20-22 MiB, never above the configured max.
- Save chunk metadata with filename, offset, duration, and size.
- Transcribe local chunk files with OpenAI.
- Use `whisper-1` as the default when timestamped verbose segments are required.
- If a newer transcription model is configured and does not support verbose segments, fall back to chunk-level timing instead of crashing.
- Add chunk offsets to all segment timestamps.
- Save `transcript_source.json`, `transcript_source.srt`, and `transcript_source.txt`.
- Translate with an env-configurable prompt.
- Require strict JSON: `{"segments":[{"id":1,"target_text":"..."}]}`.
- Validate that returned IDs exactly match input IDs.
- Retry once with a repair prompt if JSON is invalid.
- Save `translation_{lang}.json` and `translation_{lang}.srt`.
- Generate TTS with OpenAI Speech API.
- Split long TTS segment text below configured token limits.
- Build a final audio timeline using silence plus segment audio at original timestamps.
- If TTS is too long, speed up within configured max and then allow only small configured overlap/trim.
- Export final voiceover as MP3/M4A.
- Leave partial outputs downloadable when they exist.

Cost and budget requirements:
- Estimate transcription cost by audio minutes.
- Estimate translation and TTS cost with token estimates.
- Keep prices in `.env.example` and environment variables so they can be updated from current OpenAI pricing.
- Label costs as estimated unless exact usage is returned.
- If `OPENAI_ADMIN_KEY` exists, use official Admin/Costs API style access for monthly spend; do not scrape the dashboard.
- If manual balance or monthly budget is configured and estimated cost exceeds it, block job start and show an Arabic warning.

Required files:
- `app.py`
- `requirements.txt`
- `packages.txt`
- `.streamlit/config.toml`
- `.env.example`
- `README.md`
- `CHECKLIST.md`
- `CODEX_PROMPT.md`
- `src/config.py`
- `src/openai_client.py`
- `src/youtube.py`
- `src/media.py`
- `src/chunking.py`
- `src/transcription.py`
- `src/translation.py`
- `src/tts.py`
- `src/subtitles.py`
- `src/cost.py`
- `src/jobs.py`
- `src/storage.py`
- `src/ui.py`
- `src/worker.py`

Implementation style:
- Keep modules small and readable.
- Prefer reliable simple code over clever abstractions.
- Use docstrings/comments only where they clarify non-obvious behavior.
- Run `python -m compileall .` after edits.
- Run a basic import smoke test.
- Start Streamlit locally when possible and report the URL.
- Keep the README clear about Streamlit Cloud limitations: files are stored on ephemeral local disk, YouTube audio must be downloaded locally before OpenAI transcription, and long videos may be impractical on the free tier.
```

