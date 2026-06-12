# Arabic YouTube / Video Voiceover Translator

A personal Streamlit app for turning an uploaded video/audio file or a YouTube link into translated subtitles and synced AI voiceover audio.

For each selected target language, the app produces exactly two final files:

- `translation_{lang}.srt` for YouTube subtitles.
- `voiceover_{lang}.mp3` or `voiceover_{lang}.m4a` for YouTube audio upload.

The dashboard UI is Arabic, RTL, and designed for one personal user on Streamlit Community Cloud.

## Run Locally

1. Install Python 3.11+.
2. Install ffmpeg:
   - Windows: `winget install Gyan.FFmpeg`
   - macOS: `brew install ffmpeg`
   - Ubuntu/Debian: `sudo apt-get install ffmpeg`
3. Create and activate a virtual environment.
4. Install dependencies:

```bash
pip install -r requirements.txt
```

5. Create `.env` from `.env.example` and set:

```bash
OPENAI_API_KEY=your_key_here
```

6. Start the app:

```bash
streamlit run app.py
```

## Deploy To Streamlit Cloud

Push this folder to a GitHub repository and deploy `app.py` in Streamlit Cloud.

Streamlit Cloud will install:

- Python packages from `requirements.txt`.
- System ffmpeg from `packages.txt`.

Set secrets in Streamlit Cloud:

```toml
OPENAI_API_KEY = "your_key_here"
OPENAI_ADMIN_KEY = ""
OPENAI_MONTHLY_BUDGET_USD = ""
OPENAI_MANUAL_AVAILABLE_BALANCE_USD = ""
```

Do not commit `.env` or `.streamlit/secrets.toml`.

**Security warning:** never commit a real OpenAI key to GitHub. For Streamlit Cloud,
store real keys only in **App settings > Secrets**. The repository's `.env.example`
contains configuration examples only, while `.env` and `.streamlit/secrets.toml` are ignored.

## How Processing Works

Upload mode saves the file in a local job folder, then ffmpeg extracts and normalizes audio. YouTube mode uses `yt-dlp` to download/extract audio only whenever possible, rather than downloading the full video.

The app launches yt-dlp as `python -m yt_dlp` with the same Python interpreter that
runs Streamlit. This is important on virtual environments and Streamlit Cloud because
the standalone `yt-dlp` executable may not be present on `PATH` even though the Python
package is installed.

OpenAI Audio API receives local audio files, not remote URLs. That is why YouTube links are first converted into local audio files.

The app compresses audio to mono, low-bitrate MP3, then splits it into chunks under the configured 25 MiB API limit. The default chunk target is 22 MiB with a safety margin to avoid edge failures.

After transcription, chunk timestamps are shifted by each chunk offset and merged into one transcript. Translations keep the same segment IDs. SRT files are written as UTF-8 and ready for YouTube upload.

For voiceover, the app merges nearby subtitle lines into natural speech blocks and schedules them sequentially on a timeline based on the original timestamps. If a block is too long, the app regenerates it once with tighter pacing, then applies a controlled speed-up without overlapping the next speech block.

The current voiceover engine groups nearby subtitle lines into natural speech blocks,
regenerates overly long blocks once, applies limited speed fitting, and schedules all
blocks sequentially to prevent a single speaker from overlapping itself. It saves a
`voiceover_{lang}_qa.json` report with timing and silence checks.

Voice preview samples are generated only when requested because each sample uses the
OpenAI Speech API. Cached samples are stored under `data/voice_samples/` and reused.

## Logging And YouTube Diagnostics

The application writes a rotating general log to:

```text
data/logs/app.log
```

Each job also writes:

```text
data/jobs/{job_id}/logs/job.log
data/jobs/{job_id}/logs/events.jsonl
```

Both job logs can be downloaded from the **سجل التشخيص** section in the dashboard.
API keys are redacted from structured logs.

YouTube failures are categorized into authentication/cookies, private or unavailable
video, JavaScript challenge runtime, network, ffmpeg, and general extraction errors.

For public videos, no cookies should normally be required. For a private, age-restricted,
or bot-check response, export a Netscape-format cookies file and set:

```env
YT_DLP_COOKIES_FILE=/secure/path/to/youtube_cookies.txt
```

Do not commit cookie files. They are ignored by this repository. A JavaScript runtime
can be selected with `YT_DLP_JS_RUNTIME`; Deno is the runtime recommended by yt-dlp.

The earlier repeated local failures were caused by checking for a global `yt-dlp`
executable while yt-dlp was installed only inside the project virtual environment.
The downloader now uses the active Python module and validates any completed audio
output before treating a nonzero tool exit as a failure.

## Streamlit Cloud Limits

Streamlit Community Cloud can run this for personal use, especially short and medium videos. It is not ideal for very long videos because free hosting has practical limits around CPU, RAM, disk, and process lifetime.

The app must download/extract the YouTube audio locally. It is not enough to "access" the YouTube URL directly because OpenAI speech-to-text expects a local audio file upload, and chunking requires local files.

On Streamlit Cloud, files are stored in the app container under:

```text
data/jobs/{job_id}/
```

That folder contains the source audio, normalized audio, chunks, transcripts, translations, TTS segments, logs, and final files. Treat it as temporary storage. Download final outputs after the job completes.

The final files shown in the dashboard are the files intended for YouTube:

- `translation_{lang}.srt`
- `voiceover_{lang}.mp3` or `voiceover_{lang}.m4a`

## Jobs, Refresh, And Resume

Job state is stored in SQLite at `data/jobs/jobs.db`, not only in `st.session_state`.

The app enforces one active job at a time. Browser refreshes keep showing the current job. Closing the browser does not stop the job as long as the Streamlit process remains alive.

If the Streamlit process restarts, previous `queued` or `running` jobs are marked `interrupted`. The UI offers **استكمال من آخر خطوة محفوظة** and reuses checkpoint files when possible.

## Costs And Budget Guard

Costs are shown as **تقديري** unless the API response exposes exact usage. Translation token usage is recorded when available. Transcription and TTS remain estimated when exact costs are not returned by the API.

If `OPENAI_ADMIN_KEY` is set, the app calls the OpenAI Organization Costs API to estimate current monthly spend. It does not scrape the OpenAI dashboard with browser automation.

Budget checks use these safer options:

- `OPENAI_MONTHLY_BUDGET_USD` with current monthly spend.
- `OPENAI_MANUAL_AVAILABLE_BALANCE_USD`.
- No budget guard if neither value is set.

If the estimated job cost exceeds available budget or balance, the app blocks the start button and shows an Arabic warning.

Pricing defaults live in `.env.example` and can be updated without code changes:

- `OPENAI_TRANSCRIPTION_USD_PER_MIN`
- `OPENAI_TEXT_INPUT_USD_PER_1M`
- `OPENAI_TEXT_OUTPUT_USD_PER_1M`
- `OPENAI_TTS_TEXT_INPUT_USD_PER_1M`
- `OPENAI_TTS_AUDIO_OUTPUT_USD_PER_1M`

Current OpenAI docs should be checked before serious use because pricing changes over time.

## Prompt Configuration

The main prompts are configurable through `.env` or Streamlit secrets:

- `OPENAI_TRANSCRIPTION_PROMPT`
- `TRANSLATION_SYSTEM_PROMPT`
- `TRANSLATION_PROMPT_TEMPLATE`
- `TRANSLATION_REPAIR_PROMPT_TEMPLATE`
- `TRANSLATION_BATCH_SIZE` (default `20`)
- `TRANSLATION_RECOVERY_BATCH_SIZE` (default `5`)
- `TTS_INSTRUCTIONS_TEMPLATE`

The translation template must keep `{target_language}` and `{segments_json}` if you customize it. The TTS template should keep `{target_language}`.

## Notes

Files on Streamlit Community Cloud should be treated as temporary. Download final outputs after each run.

Only process YouTube videos you own or have permission to process.
