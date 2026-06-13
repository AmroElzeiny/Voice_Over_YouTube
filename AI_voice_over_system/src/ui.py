from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st

from .config import LANGUAGES, Settings
from .logging_utils import tail_text


def setup_page(settings: Settings) -> None:
    st.set_page_config(page_title=settings.app_title, page_icon="🎙️", layout="wide")
    st.markdown(
        """
        <style>
        html, body, [class*="css"], .stApp {
            direction: rtl;
            text-align: right;
            font-family: "Segoe UI", Tahoma, Arial, sans-serif;
        }
        .stApp {
            background: #F8FAFC;
            color: #0F172A;
        }
        h1, h2, h3, h4, h5, h6, p, label, span {
            letter-spacing: 0;
        }
        [data-testid="stAppViewContainer"],
        [data-testid="stMain"],
        [data-testid="stMainBlockContainer"],
        [data-testid="stWidgetLabel"],
        [data-testid="stAlert"],
        [data-testid="stMarkdownContainer"],
        [data-baseweb="popover"],
        [role="tooltip"] {
            direction: rtl;
            text-align: right;
        }
        input, textarea, div[contenteditable="true"] {
            direction: rtl;
            text-align: right;
        }
        [data-testid="stHorizontalBlock"] {
            direction: rtl;
        }
        [data-testid="stMetricValue"], [data-testid="stMetricLabel"] {
            direction: rtl;
            text-align: right;
        }
        div[data-testid="stMetric"] {
            background: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-radius: 8px;
            padding: 12px;
        }
        div[data-testid="stDownloadButton"] button,
        div[data-testid="stButton"] button {
            border-radius: 8px;
            min-height: 42px;
        }
        .status-chip {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 999px;
            background: #E0F2FE;
            color: #075985;
            border: 1px solid #BAE6FD;
            font-size: 0.9rem;
        }
        .disclosure {
            background: #FFF7ED;
            border: 1px solid #FDBA74;
            color: #7C2D12;
            border-radius: 8px;
            padding: 10px 12px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def section_title(text: str) -> None:
    st.markdown(f"### {text}")


def money(value: Any) -> str:
    try:
        return f"${float(value):,.4f}"
    except (TypeError, ValueError):
        return "غير متاح"


def language_label(code: str) -> str:
    meta = LANGUAGES.get(code, {})
    return meta.get("label_ar", code)


def elapsed_text(created_at: str | None) -> str:
    if not created_at:
        return "غير متاح"
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        seconds = int((datetime.now(timezone.utc) - created).total_seconds())
    except Exception:
        return "غير متاح"
    minutes, seconds = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}س {minutes}د"
    return f"{minutes}د {seconds}ث"


def render_job_status(job: dict[str, Any] | None, dismissed_job_id: str | None = None) -> None:
    if job and job.get("job_id") == dismissed_job_id:
        st.info("تم مسح رسالة العملية السابقة. يمكنك بدء عملية جديدة.")
        return
    if not job:
        st.info("لا توجد عملية محفوظة بعد.")
        return

    status_ar = {
        "queued": "في الانتظار",
        "running": "قيد المعالجة",
        "cancel_requested": "جاري الإلغاء",
        "cancelled": "ملغاة",
        "completed": "مكتملة",
        "failed": "فشلت",
        "interrupted": "منقطعة",
    }.get(job.get("status"), job.get("status", "غير معروف"))

    st.markdown(f'<span class="status-chip">{status_ar}</span>', unsafe_allow_html=True)
    st.write(f"**الخطوة الحالية:** {job.get('current_step') or 'غير متاح'}")
    st.write(f"**الوقت المنقضي:** {elapsed_text(job.get('created_at'))}")
    st.progress(int(float(job.get("progress_percent") or 0)))

    if job.get("error_message"):
        st.error(job["error_message"])

def render_downloads(job: dict[str, Any] | None) -> None:
    if not job:
        return
    outputs = job.get("output_paths") or {}
    languages = job.get("selected_languages") or []
    if not outputs:
        st.info("لا توجد ملفات جاهزة للتحميل حتى الآن.")
        return

    for lang in languages:
        lang_outputs = outputs.get(lang) or {}
        if not lang_outputs:
            continue
        with st.container():
            st.markdown(f"#### {language_label(lang)}")
            cols = st.columns(3)
            srt_path = lang_outputs.get("srt")
            audio_path = lang_outputs.get("audio")
            qa_path = lang_outputs.get("qa")
            if srt_path and Path(srt_path).exists():
                path = Path(srt_path)
                cols[0].download_button(
                    "تحميل ملف SRT",
                    data=path.read_bytes(),
                    file_name=path.name,
                    mime="application/x-subrip",
                    help="ترجمة جاهزة للرفع على YouTube.",
                    key=f"srt-{job['job_id']}-{lang}",
                )
            else:
                cols[0].button("ملف SRT غير جاهز", disabled=True, key=f"srt-missing-{job['job_id']}-{lang}")

            if audio_path and Path(audio_path).exists():
                path = Path(audio_path)
                mime = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/mp4"
                cols[1].download_button(
                    "تحميل ملف الصوت",
                    data=path.read_bytes(),
                    file_name=path.name,
                    mime=mime,
                    help="صوت جاهز للرفع على YouTube.",
                    key=f"audio-{job['job_id']}-{lang}",
                )
            else:
                cols[1].button("ملف الصوت غير جاهز", disabled=True, key=f"audio-missing-{job['job_id']}-{lang}")

            if qa_path and Path(qa_path).exists():
                path = Path(qa_path)
                try:
                    qa_report = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    qa_report = {}
                cols[2].download_button(
                    "تحميل تقرير جودة الصوت",
                    data=path.read_bytes(),
                    file_name=path.name,
                    mime="application/json",
                    help="يوضح نتيجة فحص الصوت والتوقيت.",
                    key=f"qa-{job['job_id']}-{lang}",
                )
                if qa_report.get("warnings"):
                    st.warning(
                        "تم إنشاء الصوت، لكن توجد ملاحظات بسيطة في التزامن. "
                        "يمكنك مراجعة تقرير الجودة."
                    )
            else:
                cols[2].button(
                    "تقرير الجودة غير جاهز",
                    disabled=True,
                    key=f"qa-missing-{job['job_id']}-{lang}",
                )
            st.divider()


def render_job_history(history: list[dict[str, Any]]) -> None:
    if not history:
        return
    with st.expander("آخر عملية محفوظة"):
        for job in history[:5]:
            status = {
                "queued": "في الانتظار",
                "running": "قيد المعالجة",
                "cancel_requested": "جاري الإلغاء",
                "cancelled": "ملغاة",
                "completed": "مكتملة",
                "failed": "فشلت",
                "interrupted": "منقطعة",
            }.get(job.get("status"), str(job.get("status") or "غير معروف"))
            st.write(f"**{status}** - {job.get('source_name_or_url') or 'مصدر غير معروف'}")
            if job.get("error_message"):
                st.caption(job["error_message"])
            st.caption(f"آخر تحديث: {job.get('updated_at') or 'غير متاح'}")
            st.divider()


def render_job_diagnostics(settings: Settings, job: dict[str, Any] | None) -> None:
    if not job:
        return
    logs_path = settings.jobs_dir / job["job_id"] / "logs"
    job_log = logs_path / "job.log"
    events_log = logs_path / "events.jsonl"
    available = [path for path in (job_log, events_log) if path.exists() and path.stat().st_size > 0]
    if not available:
        return

    with st.expander("سجل التشخيص"):
        st.caption("استخدم هذا السجل لمعرفة سبب فشل YouTube أو المعالجة دون عرض مفاتيح API.")
        columns = st.columns(2)
        if job_log.exists() and job_log.stat().st_size > 0:
            columns[0].download_button(
                "تحميل السجل الكامل",
                data=job_log.read_bytes(),
                file_name=f"{job['job_id']}_job.log",
                mime="text/plain",
                key=f"job-log-{job['job_id']}",
            )
            tail = tail_text(job_log, max_bytes=12_000)
            st.code(tail, language="text")
        if events_log.exists() and events_log.stat().st_size > 0:
            columns[1].download_button(
                "تحميل سجل الأحداث JSONL",
                data=events_log.read_bytes(),
                file_name=f"{job['job_id']}_events.jsonl",
                mime="application/x-ndjson",
                key=f"events-log-{job['job_id']}",
            )
