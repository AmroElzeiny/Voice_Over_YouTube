from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .config import Settings

_LOGGERS: dict[str, logging.Logger] = {}
_KEY_PATTERN = re.compile(r"(?:sk|sess|admin)-[A-Za-z0-9_-]{12,}")


def _redact_text(value: str, settings: Settings | None = None) -> str:
    redacted = _KEY_PATTERN.sub("[REDACTED_KEY]", value)
    if settings:
        for secret in (settings.openai_api_key, settings.openai_admin_key):
            if secret:
                redacted = redacted.replace(secret, "[REDACTED_KEY]")
        for secret in (settings.yt_dlp_cookies_base64, settings.yt_dlp_proxy):
            if secret:
                redacted = redacted.replace(secret, "[REDACTED_SECRET]")
    return redacted


def _safe_value(value: Any, settings: Settings | None = None) -> Any:
    if isinstance(value, str):
        return _redact_text(value, settings)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _safe_value(child, settings) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(child, settings) for child in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact_text(str(value), settings)


class RedactingFilter(logging.Filter):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_text(str(record.msg), self.settings)
        if record.args:
            record.args = tuple(_safe_value(item, self.settings) for item in record.args)
        return True


def configure_logging(settings: Settings) -> logging.Logger:
    """Configure a rotating application log once per output path."""
    path_key = str(settings.app_log_path.resolve())
    existing = _LOGGERS.get(path_key)
    if existing:
        return existing

    settings.app_log_path.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, settings.log_level, logging.INFO)
    logger = logging.getLogger(f"voiceover_app.{abs(hash(path_key))}")
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    handler = RotatingFileHandler(
        settings.app_log_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    handler.addFilter(RedactingFilter(settings))
    logger.addHandler(handler)
    _LOGGERS[path_key] = logger
    return logger


def close_logging(settings: Settings) -> None:
    """Close one configured logger, primarily for tests and clean shutdowns."""
    path_key = str(settings.app_log_path.resolve())
    logger = _LOGGERS.pop(path_key, None)
    if not logger:
        return
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def log_event(
    settings: Settings,
    event: str,
    message: str,
    *,
    level: str = "INFO",
    job_id: str | None = None,
    job_path: Path | None = None,
    **context: Any,
) -> None:
    """Write an application log line and an optional per-job JSONL event."""
    logger = configure_logging(settings)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level.upper(),
        "event": event,
        "message": _redact_text(message, settings),
        "job_id": job_id,
        "context": _safe_value(context, settings),
    }
    log_method = getattr(logger, level.lower(), logger.info)
    log_method(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    if job_path:
        events_path = job_path / "logs" / "events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a", encoding="utf-8") as event_file:
            event_file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def tail_text(path: Path, max_bytes: int = 40_000) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as file_obj:
        size = path.stat().st_size
        if size > max_bytes:
            file_obj.seek(-max_bytes, 2)
        return file_obj.read().decode("utf-8", errors="replace")
