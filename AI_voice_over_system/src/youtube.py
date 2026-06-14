from __future__ import annotations

import base64
import binascii
import importlib.util
import json
import os
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


def _python_deno_path() -> str | None:
    try:
        import deno

        executable = Path(deno.find_deno_bin())
    except (ImportError, OSError, TypeError, ValueError):
        return None
    return str(executable) if executable.exists() else None


def _runtime_executable(runtime: str) -> str | None:
    executable_name = "qjs" if runtime == "quickjs" else runtime
    executable = shutil.which(executable_name)
    if executable:
        return executable
    if runtime == "deno":
        return _python_deno_path()
    return None


def _detect_js_runtime(settings: Settings) -> str | None:
    configured = (settings.yt_dlp_js_runtime or "auto").strip()
    runtime_name = configured.lower()
    if runtime_name != "auto":
        if ":" in configured:
            return configured
        executable = _runtime_executable(runtime_name)
        return f"{runtime_name}:{executable}" if executable else None
    for runtime in ("deno", "node", "quickjs"):
        executable = _runtime_executable(runtime)
        if executable:
            return f"{runtime}:{executable}"
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


def _clear_failed_download(job_path: Path) -> None:
    for candidate in job_path.glob("youtube_source*"):
        if candidate.is_file():
            candidate.unlink(missing_ok=True)


def _download_strategies() -> list[dict[str, str | None]]:
    return [
        {
            "name": "default_audio",
            "format": "bestaudio/best",
            "extractor_args": None,
        },
        {
            "name": "embedded_audio",
            "format": "bestaudio/best",
            "extractor_args": "youtube:player_client=web_embedded",
        },
        {
            "name": "safari_hls",
            "format": "worst[protocol^=m3u8]/best[protocol^=m3u8]",
            "extractor_args": "youtube:player_client=web_safari",
        },
    ]


def _materialize_secret_cookies(settings: Settings) -> Path | None:
    if not settings.yt_dlp_cookies_base64:
        return None
    encoded = re.sub(r"\s+", "", settings.yt_dlp_cookies_base64)
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise YouTubeError("قيمة YT_DLP_COOKIES_BASE64 غير صالحة.") from exc
    if len(payload) > 5 * 1024 * 1024:
        raise YouTubeError("ملف YouTube cookies أكبر من الحد المسموح.")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise YouTubeError("ملف YouTube cookies يجب أن يكون نصيًا بصيغة UTF-8.") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    first_line = text.splitlines()[0].strip() if text.splitlines() else ""
    if first_line not in {"# HTTP Cookie File", "# Netscape HTTP Cookie File"}:
        raise YouTubeError("ملف YouTube cookies ليس بصيغة Netscape الصحيحة.")

    private_dir = settings.base_dir / "data" / "private"
    private_dir.mkdir(parents=True, exist_ok=True)
    cookies_path = private_dir / "youtube_cookies.txt"
    normalized = text.rstrip("\n") + "\n"
    if not cookies_path.exists() or cookies_path.read_text(encoding="utf-8") != normalized:
        cookies_path.write_text(normalized, encoding="utf-8", newline="\n")
    try:
        os.chmod(cookies_path, 0o600)
    except OSError:
        pass
    return cookies_path


def _classify_failure(stderr: str) -> tuple[str, str]:
    lowered = stderr.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered or "cookies" in lowered:
        return (
            "youtube_auth_required",
            "طلب YouTube تسجيل الدخول لأن خادم الاستضافة محظور. أضف YT_DLP_COOKIES_BASE64 وYT_DLP_USER_AGENT في Streamlit Secrets.",
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
            "تعذر تشغيل حماية JavaScript الخاصة بـ YouTube. أعد تشغيل التطبيق بعد اكتمال تثبيت Deno.",
        )
    if "http error 403" in lowered or "403: forbidden" in lowered:
        return (
            "youtube_forbidden",
            "رفض YouTube كل طرق التنزيل من خادم الاستضافة. ارفع ملف الفيديو مباشرة، أو استخدم cookies صالحة مع مزود PO Token.",
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


def _access_args(settings: Settings) -> list[str]:
    args: list[str] = []
    js_runtime = _detect_js_runtime(settings)
    if js_runtime:
        args.extend(["--js-runtimes", js_runtime])
    elif settings.yt_dlp_js_runtime and settings.yt_dlp_js_runtime != "auto":
        raise YouTubeError("مشغل JavaScript المحدد غير موجود. استخدم auto أو deno.")
    cookies_path = settings.yt_dlp_cookies_file or _materialize_secret_cookies(settings)
    if cookies_path:
        if not cookies_path.exists():
            raise YouTubeError("ملف YouTube cookies غير موجود.")
        args.extend(["--cookies", str(cookies_path)])
    elif settings.yt_dlp_cookies_from_browser:
        args.extend(["--cookies-from-browser", settings.yt_dlp_cookies_from_browser])
    if settings.yt_dlp_user_agent:
        args.extend(["--user-agent", settings.yt_dlp_user_agent])
    if settings.yt_dlp_proxy:
        args.extend(["--proxy", settings.yt_dlp_proxy])
    return args


def probe_youtube_duration(url: str, settings: Settings) -> float:
    """Read YouTube metadata without downloading the media file."""
    if not validate_youtube_url(url):
        raise YouTubeError("رابط YouTube غير صالح.")
    if not yt_dlp_available():
        raise YouTubeError("حزمة yt-dlp غير مثبتة.")

    args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        "--no-warnings",
        *_access_args(settings),
        url,
    ]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise YouTubeError("تعذر قراءة مدة فيديو YouTube.") from exc
    if result.returncode != 0:
        _event, message = _classify_failure(result.stderr + "\n" + result.stdout)
        raise YouTubeError(message)
    try:
        payload = json.loads(result.stdout)
        duration = float(payload.get("duration") or 0)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise YouTubeError("لم أتمكن من معرفة مدة فيديو YouTube.") from exc
    if duration <= 0:
        raise YouTubeError("مدة فيديو YouTube غير متاحة.")
    return duration


def download_youtube_audio(url: str, job_path: Path, settings: Settings, log_path: Path | None = None) -> Path:
    if not validate_youtube_url(url):
        raise YouTubeError("رابط YouTube غير صالح.")
    if not yt_dlp_available():
        raise YouTubeError("حزمة yt-dlp غير مثبتة داخل بيئة Python التي تشغّل التطبيق.")

    job_path.mkdir(parents=True, exist_ok=True)
    video_id = youtube_video_id(url)
    output_template = str(job_path / "youtube_source.%(ext)s")
    base_args = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--restrict-filenames",
        "--force-ipv4",
        "--no-progress",
        "--newline",
        "--retries",
        "5",
        "--fragment-retries",
        "5",
        "--extractor-retries",
        "3",
        "--retry-sleep",
        "http:linear=1::2",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        settings.audio_bitrate.upper(),
        "--output",
        output_template,
    ]

    js_runtime = _detect_js_runtime(settings)
    access_args = _access_args(settings)

    log_event(
        settings,
        "youtube_download_started",
        "Starting YouTube audio extraction.",
        job_id=job_path.parent.name,
        job_path=job_path.parent,
        video_id=video_id,
        python_executable=sys.executable,
        js_runtime=js_runtime or "none",
        cookies_configured=bool(
            settings.yt_dlp_cookies_file
            or settings.yt_dlp_cookies_base64
            or settings.yt_dlp_cookies_from_browser
        ),
        user_agent_configured=bool(settings.yt_dlp_user_agent),
        proxy_configured=bool(settings.yt_dlp_proxy),
    )
    combined_output: list[str] = []
    last_return_code = 1
    for attempt_number, strategy in enumerate(_download_strategies(), start=1):
        args = [*base_args, "--format", str(strategy["format"])]
        if strategy["extractor_args"]:
            args.extend(["--extractor-args", str(strategy["extractor_args"])])
        args.extend(access_args)
        args.append(url)

        log_event(
            settings,
            "youtube_download_attempt",
            "Trying a YouTube download strategy.",
            job_id=job_path.parent.name,
            job_path=job_path.parent,
            video_id=video_id,
            attempt=attempt_number,
            strategy=strategy["name"],
        )
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
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
        combined_output.extend([result.stderr, result.stdout])
        last_return_code = result.returncode
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
                strategy=strategy["name"],
                output_file=audio_path.name,
                output_bytes=audio_path.stat().st_size,
                duration_seconds=media.probe_duration(audio_path),
            )
            return audio_path
        _clear_failed_download(job_path)

    failure_output = "\n".join(combined_output)
    event, friendly_message = _classify_failure(failure_output)
    stderr_tail = re.sub(r"\s+", " ", failure_output[-2000:]).strip()
    log_event(
        settings,
        event,
        friendly_message,
        level="ERROR",
        job_id=job_path.parent.name,
        job_path=job_path.parent,
        video_id=video_id,
        return_code=last_return_code,
        stderr_tail=stderr_tail,
        js_runtime=js_runtime or "none",
        attempted_strategies=[strategy["name"] for strategy in _download_strategies()],
    )
    raise YouTubeError(friendly_message)
