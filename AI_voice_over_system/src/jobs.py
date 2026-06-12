from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings

RUNNING_STATUSES = {"queued", "running", "cancel_requested"}
FINISHED_STATUSES = {"completed", "failed", "interrupted", "cancelled"}

_STARTUP_RECOVERY_DONE = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(settings: Settings) -> None:
    with connect(settings.sqlite_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                input_type TEXT NOT NULL,
                source_name_or_url TEXT NOT NULL,
                selected_languages TEXT NOT NULL,
                status TEXT NOT NULL,
                current_step TEXT NOT NULL,
                progress_percent REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_message TEXT,
                output_paths TEXT NOT NULL DEFAULT '{}',
                estimated_cost_json TEXT NOT NULL DEFAULT '{}',
                actual_cost_json TEXT NOT NULL DEFAULT '{}',
                config_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )


def startup_recovery(settings: Settings) -> None:
    global _STARTUP_RECOVERY_DONE
    if _STARTUP_RECOVERY_DONE:
        return
    init_db(settings)
    with connect(settings.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = 'interrupted',
                current_step = 'انقطع تشغيل التطبيق قبل اكتمال العملية',
                updated_at = ?,
                error_message = 'تم اعتبار العملية منقطعة بعد إعادة تشغيل التطبيق.'
            WHERE status IN ('queued', 'running')
            """,
            (utc_now(),),
        )
        conn.execute(
            """
            UPDATE jobs
            SET status = 'cancelled',
                current_step = 'تم إلغاء العملية',
                updated_at = ?,
                error_message = NULL
            WHERE status = 'cancel_requested'
            """,
            (utc_now(),),
        )
    _STARTUP_RECOVERY_DONE = True


def create_job(
    settings: Settings,
    input_type: str,
    source_name_or_url: str,
    selected_languages: list[str],
    estimated_cost: dict[str, Any],
    config: dict[str, Any],
) -> str:
    job_id = uuid.uuid4().hex
    now = utc_now()
    with connect(settings.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, input_type, source_name_or_url, selected_languages, status,
                current_step, progress_percent, created_at, updated_at,
                estimated_cost_json, config_json, error_message
            )
            VALUES (?, ?, ?, ?, 'queued', 'بانتظار البدء', 0, ?, ?, ?, ?, NULL)
            """,
            (
                job_id,
                input_type,
                source_name_or_url,
                json.dumps(selected_languages, ensure_ascii=False),
                now,
                now,
                json.dumps(estimated_cost, ensure_ascii=False),
                json.dumps(config, ensure_ascii=False),
            ),
        )
    return job_id


def row_to_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    job = dict(row)
    for key, fallback in {
        "selected_languages": [],
        "output_paths": {},
        "estimated_cost_json": {},
        "actual_cost_json": {},
        "config_json": {},
    }.items():
        try:
            job[key] = json.loads(job[key]) if job[key] else fallback
        except json.JSONDecodeError:
            job[key] = fallback
    return job


def get_job(settings: Settings, job_id: str) -> dict[str, Any] | None:
    with connect(settings.sqlite_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row_to_job(row)


def get_latest_job(settings: Settings) -> dict[str, Any] | None:
    with connect(settings.sqlite_path) as conn:
        row = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    return row_to_job(row)


def get_active_job(settings: Settings) -> dict[str, Any] | None:
    with connect(settings.sqlite_path) as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued', 'running', 'cancel_requested') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return row_to_job(row)


def list_recent_jobs(settings: Settings, limit: int = 10) -> list[dict[str, Any]]:
    with connect(settings.sqlite_path) as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
    return [job for row in rows if (job := row_to_job(row)) is not None]


def update_job(settings: Settings, job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utc_now()
    json_fields = {"selected_languages", "output_paths", "estimated_cost_json", "actual_cost_json", "config_json"}
    columns = []
    values = []
    for key, value in fields.items():
        if key in json_fields:
            value = json.dumps(value, ensure_ascii=False)
        columns.append(f"{key} = ?")
        values.append(value)
    values.append(job_id)
    with connect(settings.sqlite_path) as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(columns)} WHERE job_id = ?", values)


def set_running(settings: Settings, job_id: str, step: str = "بدء المعالجة") -> None:
    job = get_job(settings, job_id)
    if not job or job.get("status") in {"cancel_requested", "cancelled"}:
        return
    update_job(settings, job_id, status="running", current_step=step, progress_percent=1, error_message=None)


def set_progress(settings: Settings, job_id: str, step: str, percent: float) -> None:
    job = get_job(settings, job_id)
    if not job or job.get("status") in {"cancel_requested", "cancelled"}:
        return
    update_job(
        settings,
        job_id,
        status="running",
        current_step=step,
        progress_percent=max(0, min(100, percent)),
    )


def request_cancel(settings: Settings, job_id: str) -> None:
    job = get_job(settings, job_id)
    if not job or job.get("status") not in RUNNING_STATUSES:
        return
    update_job(
        settings,
        job_id,
        status="cancel_requested",
        current_step="جاري إيقاف العملية بأمان...",
        error_message=None,
    )


def is_cancel_requested(settings: Settings, job_id: str) -> bool:
    job = get_job(settings, job_id)
    return bool(job and job.get("status") in {"cancel_requested", "cancelled"})


def cancel_job(
    settings: Settings,
    job_id: str,
    message: str = "تم إلغاء العملية بواسطة المستخدم.",
) -> None:
    job = get_job(settings, job_id) or {}
    update_job(
        settings,
        job_id,
        status="cancelled",
        progress_percent=float(job.get("progress_percent") or 0),
        current_step="تم إلغاء العملية",
        error_message=None,
    )


def fail_job(settings: Settings, job_id: str, message: str) -> None:
    job = get_job(settings, job_id)
    if job and job.get("status") in {"cancel_requested", "cancelled"}:
        cancel_job(settings, job_id)
        return
    update_job(
        settings,
        job_id,
        status="failed",
        current_step="فشلت العملية",
        progress_percent=100,
        error_message=message,
    )


def complete_job(settings: Settings, job_id: str, output_paths: dict[str, Any], actual_cost: dict[str, Any]) -> None:
    update_job(
        settings,
        job_id,
        status="completed",
        current_step="اكتملت العملية",
        progress_percent=100,
        output_paths=output_paths,
        actual_cost_json=actual_cost,
        error_message=None,
    )
