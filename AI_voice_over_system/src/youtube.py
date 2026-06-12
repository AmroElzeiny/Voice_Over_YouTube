from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import media
from .config import Settings
from .logging_utils import log_event


class YouTubeError(RuntimeError):
    pass


def validate_youtube_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower().split(":", 1)[0]
    return host == "youtu.be" or host.endswith(".youtu.be") or host == "youtube.com" or host.endswith(".youtube.com") or host == "youtube-nocookie.com" or host.endswith(".youtube-nocookie.com")


def youtube_video_id(url: str) -> str:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        return parsed.path.strip("/").split("/")[0]
    query_id = parse_qs(parsed.query).get("v", [""])[0]
    if query_id:
        return query_id
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
        return parts[1]
    return "unknown"


def yt_dlp_available() -> bool:
    return importlib.util.find_spec("yt_dlp") is not None


def _detect_js_runtime(settings: Settings) -> str | None:
    configured = settings.yt_dlp_js_runtime
    if configured and configured != "auto":
        return configured
    for runtime in ("deno", "node", "quickjs"):
        if shutil.which(runtime):
            return runtime
    return None


def _candidate_audio(job_path: Path) -> Path | None:
    candidates: list[Path] = []
    for suffix in (".mp3", ".m4a", ".aac", ".opus", ".webm", ".ogg"):
        candidates.extend(job_path.glob(f"youtube_source*{suffix}"))
    for candidate in sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True):
        if candidate.name.endswith(".part") or candidate.stat().st_size < 1024:
            continue
        try:
            if media.probe_duration(candidate) > 0:
                return candidate
        except Exception:
            continue
    return None


def _classify_failure(stderr: str) -> tuple[str, str]:
    lowered = stderr.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered or "cookies" in lowered:
        return (
            "youtube_auth_required",
            "طلب YouTube تسجيل الدخول أو التحقق. أضف ملف cookies صالحًا عبر YT_DLP_COOKIES_FILE.",
        )
    if "private video" in lowered or "members-only" in lowered or "age-restricted" in lowered:
        return ("youtube_restricted", "الفيديو خاص أو مقيّد ويحتاج صلاحية وحساب YouTube مناسبًا.")
    if "video unavailable" in lowered or "this video is unavailable" in lowered:
        return ("youtube_unavailable", "الفيديو غير متاح أو محذوف أو محجوب في موقع الخادم.")
    if "unsupported url" in lowered:
        return ("youtube_unsupported_url", "صيغة رابط YouTube غير مدعومة.")
    if "javascript runtime" in lowered or "challenge" in lowered:
        return (
            "youtube_js_runtime",
            "تعذر حل حماية JavaScript الخاصة بـ YouTube. ثبّت Deno أو اضبط YT_DLP_JS_RUNTIME.",
        )
    if "ffmpeg" in lowered and ("not found" in lowered or "not installed" in lowered):
        return ("youtube_ffmpeg_missing", "تم تنزيل الصوت لكن ffmpeg غير متاح لتحويله.")
    if "timed out" in lowered or "unable to download" in lowered or "network" in lowered:
        return ("youtube_network", "تعذر الاتصال بـ YouTube أو انقطع التنزيل. حاول مرة أخرى.")
    return ("youtube_download_failed", "تعذر استخراج الصوت من YouTube. راجع سجل التشخيص للتفاصيل.")


def _append_command_log(log_path: Path | None, args: list[str], stdout: str, stderr: str) -> None:
    if not log_path:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    safe_args = ["<youtube-url>" if arg.startswith("http") else arg for arg in args]
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("$ " + " ".join(safe_args) + "\n")
        if stdout:
            log_file.write(stdout + "\n")
        if stderr:
            log_file.write(stderr + "\n")


def download_youtube_audio(url: str, job_path: Path, settings: Settings, log_path: Path | None = None) -> Path:
    if not validate_youtube_url(url):
        raise YouTubeError("رابط YouTube غير صالح.")
    if not yt_dlp_available():
        raise YouTubeError("حزمة yt-dlp غير مثبتة داخل بيئة Python التي تشغّل التطبيق.")

    job_path.mkdir(parents=True, exist_ok=True)
    video_id = youtube_video_id(url)
    output_template = str(job_path / "youtube_source.%(ext)s")
    args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--restrict-filenames",
        "--no-progress",
        "--newline",
        "--format",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        settings.audio_bitrate.upper(),
        "--output",
        output_template,
    ]

    js_runtime = _detect_js_runtime(settings)
    if js_runtime:
        args.extend(["--js-runtimes", js_runtime])
    if settings.yt_dlp_cookies_file:
        if not settings.yt_dlp_cookies_file.exists():
            raise YouTubeError("ملف YouTube cookies المضبوط غير موجود.")
        args.extend(["--cookies", str(settings.yt_dlp_cookies_file)])
    elif settings.yt_dlp_cookies_from_browser:
        args.extend(["--cookies-from-browser", settings.yt_dlp_cookies_from_browser])
    args.append(url)

    log_event(
        settings,
        "youtube_download_started",
        "Starting YouTube audio extraction.",
        job_id=job_path.parent.name,
        job_path=job_path.parent,
        video_id=video_id,
        python_executable=sys.executable,
        js_runtime=js_runtime or "none",
        cookies_configured=bool(settings.yt_dlp_cookies_file or settings.yt_dlp_cookies_from_browser),
    )
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except OSError as exc:
        log_event(
            settings,
            "youtube_process_start_failed",
            str(exc),
            level="ERROR",
            job_id=job_path.parent.name,
            job_path=job_path.parent,
            video_id=video_id,
        )
        raise YouTubeError("تعذر تشغيل أداة تنزيل YouTube داخل بيئة التطبيق.") from exc

    _append_command_log(log_path, args, result.stdout, result.stderr)
    audio_path = _candidate_audio(job_path)
    if audio_path:
        log_event(
            settings,
            "youtube_download_completed",
            "YouTube audio file validated.",
            job_id=job_path.parent.name,
            job_path=job_path.parent,
            video_id=video_id,
            return_code=result.returncode,
            output_file=audio_path.name,
            output_bytes=audio_path.stat().st_size,
            duration_seconds=media.probe_duration(audio_path),
        )
        return audio_path

    event, friendly_message = _classify_failure(result.stderr + "\n" + result.stdout)
    stderr_tail = re.sub(r"\s+", " ", (result.stderr or result.stdout)[-2000:]).strip()
    log_event(
        settings,
        event,
        friendly_message,
        level="ERROR",
        job_id=job_path.parent.name,
        job_path=job_path.parent,
        video_id=video_id,
        return_code=result.returncode,
        stderr_tail=stderr_tail,
        js_runtime=js_runtime or "none",
    )
    raise YouTubeError(friendly_message)
