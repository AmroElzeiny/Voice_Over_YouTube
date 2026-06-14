from __future__ import annotations

import base64
import binascii
import functools
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from . import media
from .config import Settings
from .logging_utils import log_event

YOUTUBE_AUTH_REQUIRED = "youtube_auth_required"
YOUTUBE_IP_REPUTATION_BLOCK = "youtube_ip_reputation_block"
YOUTUBE_POT_REQUIRED = "youtube_pot_required"
YOUTUBE_POT_PROVIDER_MISSING = "youtube_pot_provider_missing"
YOUTUBE_COOKIE_INVALID = "youtube_cookie_invalid"
YOUTUBE_MEDIA_403 = "youtube_media_403"
YOUTUBE_VIDEO_RESTRICTED = "youtube_video_restricted"
YOUTUBE_VIDEO_UNAVAILABLE = "youtube_video_unavailable"
YOUTUBE_EJS_MISSING = "youtube_ejs_missing"
YOUTUBE_NETWORK_ERROR = "youtube_network_error"

LOCAL_AUDIO_FAILURES = {
    YOUTUBE_AUTH_REQUIRED,
    YOUTUBE_IP_REPUTATION_BLOCK,
    YOUTUBE_POT_REQUIRED,
    YOUTUBE_POT_PROVIDER_MISSING,
    YOUTUBE_COOKIE_INVALID,
    YOUTUBE_MEDIA_403,
}

_STARTUP_DIAGNOSTICS_LOGGED = False


class YouTubeError(RuntimeError):
    def __init__(
        self,
        message: str,
        failure_type: str = YOUTUBE_NETWORK_ERROR,
        *,
        needs_local_audio: bool = False,
    ):
        super().__init__(message)
        self.failure_type = failure_type
        self.needs_local_audio = needs_local_audio


def validate_youtube_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower().split(":", 1)[0]
    return host == "youtu.be" or host.endswith(
        (".youtu.be", ".youtube.com", ".youtube-nocookie.com")
    ) or host in {"youtube.com", "youtube-nocookie.com"}


def youtube_video_id(url: str) -> str:
    parsed = urlparse(url.strip())
    if "youtu.be" in parsed.netloc.lower():
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


def _distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _python_deno_path() -> str | None:
    try:
        import deno

        executable = Path(deno.find_deno_bin())
    except (ImportError, OSError, TypeError, ValueError):
        return None
    return str(executable) if executable.exists() else None


def _detect_js_runtime(settings: Settings) -> str | None:
    configured = (settings.yt_dlp_js_runtime or "auto").strip()
    runtime_name = configured.lower()
    if runtime_name != "auto":
        if ":" in configured:
            return configured
        executable = shutil.which("qjs" if runtime_name == "quickjs" else runtime_name)
        if not executable and runtime_name == "deno":
            executable = _python_deno_path()
        return f"{runtime_name}:{executable}" if executable else None
    for runtime_name, executable_name in (("deno", "deno"), ("node", "node"), ("quickjs", "qjs")):
        executable = shutil.which(executable_name)
        if not executable and runtime_name == "deno":
            executable = _python_deno_path()
        if executable:
            return f"{runtime_name}:{executable}"
    return None


@functools.lru_cache(maxsize=8)
def _deno_version(runtime_spec: str | None) -> str | None:
    if not runtime_spec or not runtime_spec.startswith("deno:"):
        return None
    try:
        result = subprocess.run(
            [runtime_spec.split(":", 1)[1], "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first_line = (result.stdout or result.stderr).splitlines()
    return first_line[0].strip() if result.returncode == 0 and first_line else None


def _bgutil_plugin_installed() -> bool:
    return _distribution_version("bgutil-ytdlp-pot-provider") is not None


def _bgutil_plugin_detected() -> bool:
    for module_name in (
        "yt_dlp_plugins.extractor.getpot_bgutil",
        "yt_dlp_plugins.extractor.getpot_bgutil_http",
        "yt_dlp_plugins.extractor.getpot_bgutil_script",
    ):
        try:
            if importlib.util.find_spec(module_name) is not None:
                return True
        except (ImportError, ModuleNotFoundError):
            continue
    return False


def _provider_configuration(settings: Settings) -> dict[str, Any]:
    provider = settings.yt_dlp_pot_provider
    configured = provider != "none"
    valid = False
    extractor_arg = ""
    if provider == "bgutil_http" and settings.yt_dlp_bgutil_base_url:
        parsed = urlparse(settings.yt_dlp_bgutil_base_url)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        if valid:
            extractor_arg = f"youtubepot-bgutilhttp:base_url={settings.yt_dlp_bgutil_base_url}"
    elif provider == "bgutil_script" and settings.yt_dlp_bgutil_script_home:
        valid = settings.yt_dlp_bgutil_script_home.exists()
        if valid:
            extractor_arg = (
                "youtubepot-bgutilscript:server_home="
                f"{settings.yt_dlp_bgutil_script_home}"
            )
    plugin_installed = _bgutil_plugin_installed()
    plugin_detected = _bgutil_plugin_detected()
    return {
        "name": provider,
        "configured": configured,
        "configuration_valid": valid,
        "plugin_installed": plugin_installed,
        "plugin_detected": plugin_detected,
        "ready": configured and valid and plugin_installed and plugin_detected,
        "extractor_arg": extractor_arg,
    }


@functools.lru_cache(maxsize=1)
def _list_impersonation_targets() -> list[str]:
    if not yt_dlp_available() or importlib.util.find_spec("curl_cffi") is None:
        return []
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--list-impersonate-targets"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    targets: list[str] = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*([A-Za-z]+(?:-[\d.]+)?)\s+", line)
        if match and match.group(1).lower() not in {"client", "source"}:
            targets.append(match.group(1))
    return sorted(set(targets))


def collect_diagnostics(settings: Settings) -> dict[str, Any]:
    runtime = _detect_js_runtime(settings)
    provider = _provider_configuration(settings)
    impersonation_targets = _list_impersonation_targets()
    return {
        "yt_dlp_available": yt_dlp_available(),
        "yt_dlp_version": _distribution_version("yt-dlp"),
        "ejs_available": _distribution_version("yt-dlp-ejs") is not None,
        "ejs_version": _distribution_version("yt-dlp-ejs"),
        "deno_available": bool(runtime and runtime.startswith("deno:")),
        "deno_path": runtime.split(":", 1)[1] if runtime and runtime.startswith("deno:") else None,
        "deno_version": _deno_version(runtime),
        "curl_cffi_available": importlib.util.find_spec("curl_cffi") is not None,
        "impersonation_available": bool(impersonation_targets),
        "impersonation_targets": impersonation_targets,
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "cookies_configured": bool(settings.yt_dlp_cookies_file or settings.yt_dlp_cookies_base64),
        "proxy_configured": bool(settings.yt_dlp_proxy),
        "pot_provider_configured": provider["configured"],
        "pot_provider_plugin_installed": provider["plugin_installed"],
        "pot_provider_detected": provider["plugin_detected"],
        "pot_provider_ready": provider["ready"],
        "pot_provider_name": provider["name"],
        "cloud_direct_enabled": settings.yt_dlp_cloud_direct_enabled,
        "external_downloader_configured": bool(settings.youtube_external_downloader_url),
    }


def log_startup_diagnostics(settings: Settings) -> dict[str, Any]:
    global _STARTUP_DIAGNOSTICS_LOGGED
    diagnostics = collect_diagnostics(settings)
    if not _STARTUP_DIAGNOSTICS_LOGGED:
        log_event(settings, "youtube_startup_diagnostics", "YouTube capabilities checked.", **diagnostics)
        _STARTUP_DIAGNOSTICS_LOGGED = True
    return diagnostics


def _materialize_secret_cookies(settings: Settings) -> Path | None:
    if not settings.yt_dlp_cookies_base64:
        return None
    encoded = re.sub(r"\s+", "", settings.yt_dlp_cookies_base64)
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise YouTubeError(
            "قيمة YT_DLP_COOKIES_BASE64 غير صالحة.",
            YOUTUBE_COOKIE_INVALID,
            needs_local_audio=True,
        ) from exc
    if not payload or len(payload) > 5 * 1024 * 1024:
        raise YouTubeError(
            "ملف YouTube cookies فارغ أو كبير جدًا.",
            YOUTUBE_COOKIE_INVALID,
            needs_local_audio=True,
        )
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise YouTubeError(
            "ملف YouTube cookies يجب أن يكون UTF-8.",
            YOUTUBE_COOKIE_INVALID,
            needs_local_audio=True,
        ) from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    if not lines or lines[0].strip() not in {"# HTTP Cookie File", "# Netscape HTTP Cookie File"}:
        raise YouTubeError("ملف YouTube cookies ليس بصيغة Netscape.", YOUTUBE_COOKIE_INVALID, needs_local_audio=True)
    if not any(".youtube.com\t" in line for line in lines if line and not line.startswith("#")):
        raise YouTubeError("ملف cookies لا يحتوي على بيانات youtube.com.", YOUTUBE_COOKIE_INVALID, needs_local_audio=True)
    private_dir = settings.base_dir / "data" / "private"
    private_dir.mkdir(parents=True, exist_ok=True)
    cookies_path = private_dir / "youtube_cookies.txt"
    cookies_path.write_text(text.rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    try:
        os.chmod(cookies_path, 0o600)
    except OSError:
        pass
    return cookies_path


def configured_cookie_path(settings: Settings) -> Path | None:
    path = settings.yt_dlp_cookies_file or _materialize_secret_cookies(settings)
    if path and (not path.exists() or path.stat().st_size == 0):
        raise YouTubeError("ملف YouTube cookies غير موجود أو فارغ.", YOUTUBE_COOKIE_INVALID, needs_local_audio=True)
    if path:
        if path.stat().st_size > 5 * 1024 * 1024:
            raise YouTubeError("ملف YouTube cookies كبير جدًا.", YOUTUBE_COOKIE_INVALID, needs_local_audio=True)
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise YouTubeError(
                "ملف YouTube cookies يجب أن يكون UTF-8.",
                YOUTUBE_COOKIE_INVALID,
                needs_local_audio=True,
            ) from exc
        lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        if not lines or lines[0].strip() not in {"# HTTP Cookie File", "# Netscape HTTP Cookie File"}:
            raise YouTubeError("ملف YouTube cookies ليس بصيغة Netscape.", YOUTUBE_COOKIE_INVALID, needs_local_audio=True)
        if not any(".youtube.com\t" in line for line in lines if line and not line.startswith("#")):
            raise YouTubeError("ملف cookies لا يحتوي على بيانات youtube.com.", YOUTUBE_COOKIE_INVALID, needs_local_audio=True)
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            try:
                os.chmod(path, 0o600)
            except OSError as exc:
                raise YouTubeError(
                    "تعذر تأمين صلاحيات ملف YouTube cookies.",
                    YOUTUBE_COOKIE_INVALID,
                    needs_local_audio=True,
                ) from exc
    return path


def _common_args(settings: Settings, *, include_cookies: bool) -> list[str]:
    args: list[str] = []
    runtime = _detect_js_runtime(settings)
    if runtime:
        args.extend(["--js-runtimes", runtime])
    if not _distribution_version("yt-dlp-ejs") and settings.yt_dlp_allow_remote_ejs:
        args.extend(["--remote-components", "ejs:npm"])
    if include_cookies:
        cookies_path = configured_cookie_path(settings)
        if cookies_path:
            args.extend(["--cookies", str(cookies_path)])
    if settings.yt_dlp_user_agent:
        args.extend(["--user-agent", settings.yt_dlp_user_agent])
    if settings.yt_dlp_proxy:
        args.extend(["--proxy", settings.yt_dlp_proxy])
    return args


def _strategies(settings: Settings) -> list[dict[str, Any]]:
    provider = _provider_configuration(settings)
    cookies_configured = bool(settings.yt_dlp_cookies_file or settings.yt_dlp_cookies_base64)
    strategies: list[dict[str, Any]] = []
    if provider["ready"]:
        strategies.append(
            {
                "name": "configured_po_provider",
                "player_client": "mweb",
                "include_cookies": cookies_configured,
                "extra_args": [
                    "--extractor-args",
                    "youtube:player_client=mweb",
                    "--extractor-args",
                    provider["extractor_arg"],
                ],
                "provider_expected": True,
            }
        )
    if cookies_configured:
        strategies.append(
            {
                "name": "configured_cookies",
                "player_client": "default",
                "include_cookies": True,
                "extra_args": [],
                "provider_expected": False,
            }
        )
    if settings.yt_dlp_cloud_direct_enabled:
        strategies.append(
            {
                "name": "anonymous_cloud",
                "player_client": "default",
                "include_cookies": False,
                "extra_args": [],
                "provider_expected": False,
            }
        )
    return strategies[:3]


def _classify_failure(output: str, *, provider_ready: bool = False) -> tuple[str, str]:
    lowered = output.lower()
    cloud_message = (
        "رفض YouTube الطلب الصادر من خادم Streamlit. تغيير المتصفح أو إعادة المحاولة لن يضمن الحل. "
        "يمكنك رفع ملف الصوت أو استخدام أداة الاستخراج المحلية ثم متابعة نفس العملية."
    )
    if "video unavailable" in lowered or "this video is unavailable" in lowered:
        return YOUTUBE_VIDEO_UNAVAILABLE, "الفيديو غير متاح أو محذوف أو محجوب في موقع الخادم."
    if "private video" in lowered or "members-only" in lowered or "age-restricted" in lowered:
        return YOUTUBE_VIDEO_RESTRICTED, "الفيديو خاص أو مقيّد ويحتاج صلاحية مناسبة."
    if "account cookies" in lowered or "cookies are no longer valid" in lowered:
        return YOUTUBE_COOKIE_INVALID, cloud_message
    if "sign in to confirm" in lowered or "not a bot" in lowered:
        return YOUTUBE_IP_REPUTATION_BLOCK, cloud_message
    if "po token" in lowered:
        if provider_ready:
            return YOUTUBE_POT_REQUIRED, cloud_message
        return YOUTUBE_POT_PROVIDER_MISSING, cloud_message
    if "http error 403" in lowered or "403: forbidden" in lowered or "fragment not found" in lowered:
        return YOUTUBE_MEDIA_403, cloud_message
    if "javascript runtime" in lowered or "ejs" in lowered and "missing" in lowered:
        return YOUTUBE_EJS_MISSING, "تعذر تشغيل دعم JavaScript الخاص بـ YouTube."
    return YOUTUBE_NETWORK_ERROR, "تعذر الاتصال بـ YouTube أو انقطع التنزيل. حاول لاحقًا أو ارفع الصوت."


def _provider_reported(output: str) -> bool:
    lowered = output.lower()
    return "po token providers:" in lowered and "bgutil" in lowered


def _provider_used(output: str) -> bool:
    lowered = output.lower()
    return _provider_reported(output) and any(
        marker in lowered for marker in ("minting", "generating po token", "provided po token")
    )


def _selected_format_id(output: str) -> str | None:
    match = re.search(r"Downloading\s+1\s+format\(s\):\s+([^\s]+)", output)
    return match.group(1) if match else None


def _safe_tool_output(value: str, settings: Settings) -> str:
    safe = re.sub(
        r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s\"'<>]+",
        "<youtube-url>",
        value,
        flags=re.IGNORECASE,
    )
    safe = re.sub(r"https?://[^\s/@]+:[^\s/@]+@", "https://[REDACTED]@", safe)
    safe = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_ACCOUNT]", safe)
    for secret in (
        settings.yt_dlp_cookies_base64,
        settings.yt_dlp_proxy,
        settings.youtube_external_downloader_token,
    ):
        if secret:
            safe = safe.replace(secret, "[REDACTED_SECRET]")
    return safe


def _safe_command_log(
    log_path: Path | None,
    args: list[str],
    stdout: str,
    stderr: str,
    settings: Settings,
) -> None:
    if not log_path:
        return
    safe_args: list[str] = []
    hide_next = False
    for arg in args:
        if hide_next:
            safe_args.append("[REDACTED]")
            hide_next = False
        elif arg in {"--cookies", "--proxy"}:
            safe_args.append(arg)
            hide_next = True
        elif arg.startswith("http"):
            safe_args.append("<youtube-url>")
        else:
            safe_args.append(arg)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("$ " + " ".join(safe_args) + "\n")
        if stdout:
            log_file.write(_safe_tool_output(stdout, settings)[-12000:] + "\n")
        if stderr:
            log_file.write(_safe_tool_output(stderr, settings)[-12000:] + "\n")


def _candidate_audio(job_path: Path) -> Path | None:
    for candidate in sorted(job_path.glob("youtube_source.*"), key=lambda item: item.stat().st_mtime, reverse=True):
        if candidate.suffix == ".part" or candidate.stat().st_size < 1024:
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


def _download_external(
    url: str,
    job_id: str,
    job_path: Path,
    settings: Settings,
) -> Path | None:
    endpoint = settings.youtube_external_downloader_url
    if not endpoint:
        return None
    headers = {"Accept": "application/json, audio/*"}
    if settings.youtube_external_downloader_token:
        headers["Authorization"] = f"Bearer {settings.youtube_external_downloader_token}"
    try:
        response = requests.post(
            endpoint,
            json={"youtube_url": url, "job_id": job_id, "audio_format": "mp3"},
            headers=headers,
            timeout=120,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise YouTubeError(
            "تعذر الاتصال بعامل التنزيل الخارجي.",
            YOUTUBE_NETWORK_ERROR,
        ) from exc
    content_type = response.headers.get("Content-Type", "").lower()
    audio_bytes: bytes
    if "application/json" in content_type:
        try:
            payload = response.json()
        except requests.JSONDecodeError as exc:
            raise YouTubeError(
                "عامل التنزيل الخارجي أعاد استجابة غير صالحة.",
                YOUTUBE_NETWORK_ERROR,
            ) from exc
        signed_url = str(payload.get("signed_url") or "")
        if not signed_url:
            raise YouTubeError("عامل التنزيل الخارجي لم يُرجع ملفًا.", YOUTUBE_NETWORK_ERROR)
        try:
            signed_response = requests.get(signed_url, timeout=120)
            signed_response.raise_for_status()
        except requests.RequestException as exc:
            raise YouTubeError(
                "تعذر تحميل الملف من عامل التنزيل الخارجي.",
                YOUTUBE_NETWORK_ERROR,
            ) from exc
        audio_bytes = signed_response.content
    else:
        audio_bytes = response.content
    if len(audio_bytes) < 1024:
        raise YouTubeError("عامل التنزيل الخارجي أعاد ملفًا فارغًا.", YOUTUBE_NETWORK_ERROR)
    output = job_path / "youtube_source.mp3"
    output.write_bytes(audio_bytes)
    if media.probe_duration(output) <= 0:
        output.unlink(missing_ok=True)
        raise YouTubeError("ملف عامل التنزيل الخارجي غير صالح.", YOUTUBE_NETWORK_ERROR)
    return output


def download_youtube_audio(
    url: str,
    job_path: Path,
    settings: Settings,
    log_path: Path | None = None,
) -> Path:
    if not validate_youtube_url(url):
        raise YouTubeError("رابط YouTube غير صالح.", YOUTUBE_VIDEO_UNAVAILABLE)
    if not yt_dlp_available():
        raise YouTubeError("حزمة yt-dlp غير مثبتة.", YOUTUBE_NETWORK_ERROR)
    job_path.mkdir(parents=True, exist_ok=True)
    video_id = youtube_video_id(url)
    job_id = job_path.parent.name
    try:
        external = _download_external(url, job_id, job_path, settings)
    except YouTubeError as exc:
        log_event(
            settings,
            "youtube_external_download_failed",
            str(exc),
            level="WARNING",
            job_id=job_id,
            job_path=job_path.parent,
            failure_type=exc.failure_type,
        )
        external = None
    if external:
        log_event(settings, "youtube_external_download_completed", "External downloader returned audio.", job_id=job_id)
        return external

    diagnostics = collect_diagnostics(settings)
    provider = _provider_configuration(settings)
    if not diagnostics["ejs_available"] and not settings.yt_dlp_allow_remote_ejs:
        raise YouTubeError("دعم EJS غير متاح داخل التطبيق.", YOUTUBE_EJS_MISSING)
    strategies = _strategies(settings)
    if not strategies:
        raise YouTubeError(
            "التنزيل المباشر من YouTube غير مفعّل. ارفع ملف الصوت.",
            YOUTUBE_IP_REPUTATION_BLOCK,
            needs_local_audio=True,
        )

    output_template = str(job_path / "youtube_source.%(ext)s")
    combined_outputs: list[str] = []
    last_failure = YOUTUBE_NETWORK_ERROR
    last_message = "تعذر تنزيل الصوت من YouTube."
    for attempt_number, strategy in enumerate(strategies[:3], start=1):
        args = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--verbose",
            "--no-playlist",
            "--restrict-filenames",
            "--no-progress",
            "--newline",
            "--retries",
            str(settings.yt_dlp_retries),
            "--fragment-retries",
            str(settings.yt_dlp_fragment_retries),
            "--extractor-retries",
            str(settings.yt_dlp_extractor_retries),
            "--abort-on-unavailable-fragments",
            "--format",
            "bestaudio/best",
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            settings.audio_bitrate.upper(),
            "--output",
            output_template,
            *strategy["extra_args"],
            *_common_args(settings, include_cookies=bool(strategy["include_cookies"])),
            url,
        ]
        log_event(
            settings,
            "youtube_download_attempt",
            "Trying one configured YouTube strategy.",
            job_id=job_id,
            job_path=job_path.parent,
            video_id=video_id,
            attempt=attempt_number,
            strategy=strategy["name"],
            yt_dlp_version=diagnostics["yt_dlp_version"],
            ejs_available=diagnostics["ejs_available"],
            js_runtime=diagnostics["deno_version"] or "unavailable",
            impersonation_available=diagnostics["impersonation_available"],
            cookies_configured=bool(strategy["include_cookies"]),
            proxy_configured=diagnostics["proxy_configured"],
            pot_provider_configured=provider["configured"],
            pot_provider_detected=provider["plugin_detected"],
            selected_player_client=strategy["player_client"],
        )
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            output = str(exc)
            result_code = -1
        else:
            output = f"{result.stderr}\n{result.stdout}"
            result_code = result.returncode
            _safe_command_log(log_path, args, result.stdout, result.stderr, settings)
        combined_outputs.append(output)
        provider_reported = _provider_reported(output)
        provider_used = _provider_used(output)
        selected_format = _selected_format_id(output)
        audio_path = _candidate_audio(job_path)
        if audio_path:
            log_event(
                settings,
                "youtube_download_completed",
                "YouTube audio validated.",
                job_id=job_id,
                job_path=job_path.parent,
                video_id=video_id,
                attempt=attempt_number,
                strategy=strategy["name"],
                selected_format_id=selected_format,
                pot_provider_reported=provider_reported,
                pot_provider_used=provider_used,
                output_bytes=audio_path.stat().st_size,
            )
            return audio_path
        _clear_failed_download(job_path)
        last_failure, last_message = _classify_failure(output, provider_ready=bool(provider["ready"]))
        log_event(
            settings,
            "youtube_download_attempt_failed",
            last_message,
            level="WARNING",
            job_id=job_id,
            job_path=job_path.parent,
            video_id=video_id,
            attempt=attempt_number,
            strategy=strategy["name"],
            return_code=result_code,
            failure_type=last_failure,
            selected_format_id=selected_format,
            pot_provider_reported=provider_reported,
            pot_provider_used=provider_used,
            stderr_tail=re.sub(r"\s+", " ", _safe_tool_output(output, settings)[-2000:]).strip(),
        )
        if last_failure in {YOUTUBE_VIDEO_UNAVAILABLE, YOUTUBE_VIDEO_RESTRICTED, YOUTUBE_EJS_MISSING}:
            break
        if attempt_number < len(strategies):
            time.sleep(1)

    needs_local = last_failure in LOCAL_AUDIO_FAILURES
    raise YouTubeError(last_message, last_failure, needs_local_audio=needs_local)
