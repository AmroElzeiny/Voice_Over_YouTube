from __future__ import annotations

import shutil
import time

import streamlit as st

from src import cost, jobs, preflight, storage, tts, ui, worker, youtube
from src.config import LANGUAGES, TTS_STYLES, TTS_VOICES, load_settings
from src.logging_utils import configure_logging, log_event


def render_openai_account_status(settings) -> None:
    summary = cost.recorded_jobs_summary(jobs.list_all_jobs(settings))
    with st.expander("حالة حساب OpenAI"):
        columns = st.columns(3)
        columns[0].metric("تكلفة كل الملفات", ui.money(summary["total_usd"]))
        columns[1].metric("الرموز المحسوبة", f"{int(summary['total_billable_tokens']):,}")
        if settings.openai_manual_available_balance_usd is not None:
            columns[2].metric("الرصيد المتاح", ui.money(settings.openai_manual_available_balance_usd))
        else:
            columns[2].metric("الرصيد المتاح", "غير مدعوم عبر API")
        st.caption(f"عدد العمليات المحسوبة: {int(summary['job_count'])}")


def main() -> None:
    settings = load_settings()
    storage.ensure_storage(settings)
    configure_logging(settings)
    jobs.startup_recovery(settings)
    ui.setup_page(settings)
    log_event(settings, "app_rendered", "Streamlit app rendered.")

    st.title(settings.app_title)
    st.markdown(
        '<div class="disclosure">الصوت الناتج تم إنشاؤه بالذكاء الاصطناعي.</div>',
        unsafe_allow_html=True,
    )

    latest_job = jobs.get_latest_job(settings)
    active_job = jobs.get_active_job(settings)
    dismissed_job_id = st.session_state.get("dismissed_job_id")

    with st.container(border=True):
        ui.section_title("مصدر الفيديو")
        input_mode = st.radio(
            "اختر المصدر",
            ["رفع ملف", "رابط YouTube"],
            horizontal=True,
            help="ارفع ملفًا أو ضع رابط YouTube.",
        )
        uploaded_file = None
        youtube_url = ""
        if input_mode == "رفع ملف":
            uploaded_file = st.file_uploader(
                "ارفع ملف فيديو أو صوت",
                type=["mp4", "mov", "mkv", "webm", "mp3", "m4a", "aac", "wav", "flac", "ogg", "opus"],
                help="سيقرأ التطبيق الصوت من هذا الملف.",
            )
            if uploaded_file and uploaded_file.size > settings.max_upload_mb * 1024 * 1024:
                st.error(f"حجم الملف أكبر من الحد المسموح: {settings.max_upload_mb} MB.")
        else:
            youtube_url = st.text_input(
                "رابط YouTube",
                placeholder="https://www.youtube.com/watch?v=...",
                help="ضع رابط فيديو YouTube صالحًا.",
            )
            st.info("استخدم فقط الفيديوهات التي تملكها أو لديك إذن واضح لمعالجتها.")

        source_signature = (
            input_mode,
            uploaded_file.name if uploaded_file else "",
            uploaded_file.size if uploaded_file else 0,
            youtube_url.strip(),
        )
        previous_signature = st.session_state.get("source_signature")
        if (
            previous_signature is not None
            and previous_signature != source_signature
            and latest_job
            and latest_job.get("status") in jobs.FINISHED_STATUSES
            and not active_job
        ):
            st.session_state["dismissed_job_id"] = latest_job["job_id"]
            dismissed_job_id = latest_job["job_id"]
        st.session_state["source_signature"] = source_signature

    with st.container(border=True):
        ui.section_title("إعدادات اللغة والصوت")
        selected_languages = st.multiselect(
            "اللغات المطلوبة",
            options=list(LANGUAGES.keys()),
            default=[code for code in settings.default_target_languages if code in LANGUAGES],
            format_func=lambda code: LANGUAGES[code]["label_ar"],
            help="ستحصل على ترجمة وصوت لكل لغة.",
        )
        voice = st.selectbox(
            "الصوت",
            options=TTS_VOICES,
            index=TTS_VOICES.index(settings.openai_tts_voice)
            if settings.openai_tts_voice in TTS_VOICES
            else 0,
            help="اختر الصوت الذي تفضله.",
        )
        voice_style = st.selectbox(
            "أسلوب الصوت",
            options=list(TTS_STYLES.keys()),
            index=list(TTS_STYLES.keys()).index(settings.tts_naturalness_style),
            format_func=lambda style_code: TTS_STYLES[style_code]["label_ar"],
            help="اختر طريقة قراءة النص.",
        )
        st.caption(
            "لجودة صوت أفضل، سيقوم التطبيق بدمج الجمل القصيرة في مقاطع صوتية أطول "
            "بدل قراءة كل سطر وحده."
        )

        with st.expander("عينات الأصوات"):
            st.caption(
                "استمع لعينة قصيرة قبل اختيار الصوت. إنشاء العينات يستخدم OpenAI "
                "وقد يضيف تكلفة بسيطة."
            )
            existing_samples = {
                sample_voice: tts.voice_sample_path(
                    settings, sample_voice, settings.voice_samples_dir
                )
                for sample_voice in TTS_VOICES
            }
            if st.button(
                "إنشاء عينات الأصوات",
                disabled=not settings.openai_api_key,
                help="ينشئ مقطعًا قصيرًا لكل صوت.",
            ):
                try:
                    with st.spinner("جاري إنشاء عينات الأصوات..."):
                        for sample_voice in TTS_VOICES:
                            tts.generate_voice_sample(
                                settings,
                                sample_voice,
                                tts.VOICE_SAMPLE_TEXT,
                                settings.voice_samples_dir,
                            )
                    st.success("تم إنشاء عينات الأصوات.")
                except Exception:
                    st.error("تعذر إنشاء بعض عينات الأصوات. تحقق من مفتاح OpenAI والرصيد.")
            if not settings.openai_api_key:
                st.info("أضف مفتاح OpenAI أولًا لإنشاء عينات الأصوات.")
            sample_columns = st.columns(3)
            shown_samples = 0
            for sample_index, (sample_voice, sample_path) in enumerate(existing_samples.items()):
                if sample_path.exists() and sample_path.stat().st_size > 0:
                    with sample_columns[sample_index % 3]:
                        st.write(f"**{sample_voice}**")
                        st.audio(sample_path.read_bytes(), format="audio/mp3")
                    shown_samples += 1
            if shown_samples == 0:
                st.caption("لا توجد عينات محفوظة بعد.")
        output_format = st.selectbox(
            "صيغة الصوت النهائية",
            options=["mp3", "m4a"],
            index=0 if settings.output_audio_format != "m4a" else 1,
            help="MP3 هو الخيار الأسهل. M4A يعمل أيضًا.",
        )

    confirmation_signature = (
        *source_signature,
        tuple(selected_languages),
        voice,
        voice_style,
        output_format,
    )
    pending_confirmation = st.session_state.get("start_confirmation")
    if pending_confirmation and pending_confirmation.get("signature") != confirmation_signature:
        st.session_state.pop("start_confirmation", None)
        pending_confirmation = None

    with st.container(border=True):
        ui.section_title("بدء العملية")
        source_ready = bool(uploaded_file) if input_mode == "رفع ملف" else youtube.validate_youtube_url(youtube_url)
        file_size_ok = True
        if uploaded_file:
            file_size_ok = uploaded_file.size <= settings.max_upload_mb * 1024 * 1024
        ffmpeg_ready = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
        if not ffmpeg_ready:
            st.error("ffmpeg غير متاح. محليًا ثبّته على الجهاز، وفي Streamlit Cloud تأكد من packages.txt.")
        if not settings.openai_api_key:
            st.error("مفتاح OpenAI غير موجود. أضف OPENAI_API_KEY في Streamlit secrets أو ملف .env.")
        if active_job:
            if active_job.get("status") == "cancel_requested":
                st.warning("جاري إيقاف العملية بأمان بعد انتهاء الخطوة الحالية.")
            else:
                st.warning("توجد عملية نشطة حاليًا. يمكنك انتظار اكتمالها أو إلغاؤها.")
                if st.button(
                    "إلغاء العملية الحالية",
                    type="secondary",
                    help="يوقف العملية بعد إنهاء الخطوة الحالية.",
                ):
                    jobs.request_cancel(settings, active_job["job_id"])
                    st.warning("سيتم إيقاف العملية بأمان بعد انتهاء الخطوة الحالية.")
                    st.rerun()

        can_prepare = (
            settings.openai_api_key
            and ffmpeg_ready
            and source_ready
            and file_size_ok
            and bool(selected_languages)
            and not active_job
        )
        if not pending_confirmation:
            if st.button(
                "بدء العملية",
                type="primary",
                disabled=not can_prepare,
                help="سيحسب التطبيق المدة والتكلفة أولًا.",
            ):
                try:
                    with st.spinner("جاري قراءة مدة الملف وحساب التكلفة..."):
                        if uploaded_file:
                            duration_seconds = preflight.probe_uploaded_duration(
                                uploaded_file,
                                uploaded_file.name,
                            )
                        else:
                            duration_seconds = youtube.probe_youtube_duration(youtube_url, settings)
                        estimate = cost.estimate_from_minutes(
                            settings,
                            duration_seconds / 60,
                            len(selected_languages),
                        )
                        budget = cost.budget_status(settings, estimate, None)
                    pending_confirmation = {
                        "signature": confirmation_signature,
                        "duration_seconds": duration_seconds,
                        "estimate": estimate,
                        "budget": budget,
                    }
                    st.session_state["start_confirmation"] = pending_confirmation
                    log_event(
                        settings,
                        "start_confirmation_ready",
                        "Source duration and estimated total cost are ready for confirmation.",
                        duration_seconds=duration_seconds,
                        estimated_total_usd=estimate.get("total_usd"),
                        language_count=len(selected_languages),
                    )
                except Exception as exc:
                    log_event(
                        settings,
                        "start_confirmation_failed",
                        str(exc),
                        level="ERROR",
                        exception_type=type(exc).__name__,
                    )
                    st.error(str(exc) or "تعذر قراءة مدة الملف.")

        if pending_confirmation:
            estimate = pending_confirmation["estimate"]
            budget = pending_confirmation["budget"]
            duration_minutes = float(pending_confirmation["duration_seconds"]) / 60
            st.success(
                f"مدة الملف: {duration_minutes:.1f} دقيقة\n\n"
                f"**التكلفة المتوقعة: {ui.money(estimate.get('total_usd'))}**"
            )
            if not budget.get("allowed", True):
                st.error("الرصيد أو الميزانية أقل من التكلفة المتوقعة.")

            confirm_col, back_col = st.columns(2)
            if confirm_col.button(
                "متابعة وبدء العمل",
                type="primary",
                disabled=not budget.get("allowed", True),
                help="يبدأ التفريغ والترجمة وإنشاء الصوت.",
            ):
                input_type = "upload" if input_mode == "رفع ملف" else "youtube"
                source_name = uploaded_file.name if uploaded_file else youtube_url
                config = {
                    "voice": voice,
                    "voice_style": voice_style,
                    "output_format": output_format,
                    "preflight_duration_seconds": pending_confirmation["duration_seconds"],
                }
                if uploaded_file:
                    safe_name = storage.safe_filename(uploaded_file.name)
                    config["source_file"] = safe_name
                job_id = jobs.create_job(
                    settings,
                    input_type=input_type,
                    source_name_or_url=source_name,
                    selected_languages=selected_languages,
                    estimated_cost=estimate,
                    config=config,
                )
                if uploaded_file:
                    uploaded_file.seek(0)
                    storage.save_uploaded_file(
                        uploaded_file,
                        storage.source_dir(settings, job_id) / config["source_file"],
                    )
                worker.start_job_worker(settings, job_id)
                st.session_state.pop("start_confirmation", None)
                st.session_state.pop("dismissed_job_id", None)
                st.success("بدأت العملية.")
                st.rerun()

            if back_col.button(
                "رجوع",
                help="يرجع إلى الإعدادات دون بدء العملية.",
            ):
                st.session_state.pop("start_confirmation", None)
                st.rerun()

        render_openai_account_status(settings)

        if (
            latest_job
            and latest_job.get("status") == "interrupted"
            and not active_job
            and dismissed_job_id != latest_job.get("job_id")
        ):
            if st.button(
                "استكمال من آخر خطوة محفوظة",
                help="يكمل من آخر خطوة محفوظة.",
            ):
                jobs.update_job(
                    settings,
                    latest_job["job_id"],
                    status="queued",
                    current_step="استئناف العملية",
                    error_message=None,
                )
                worker.start_job_worker(settings, latest_job["job_id"])
                st.rerun()

        if (
            latest_job
            and latest_job.get("status") in jobs.FINISHED_STATUSES
            and not active_job
            and dismissed_job_id != latest_job.get("job_id")
        ):
            if st.button(
                "مسح الرسالة والبدء من جديد",
                help="يخفي رسالة العملية السابقة.",
            ):
                st.session_state["dismissed_job_id"] = latest_job["job_id"]
                st.rerun()

    with st.container(border=True):
        ui.section_title("حالة العملية")
        latest_job = jobs.get_latest_job(settings)
        ui.render_job_status(latest_job, st.session_state.get("dismissed_job_id"))
        ui.render_job_diagnostics(settings, latest_job)
        ui.render_job_history(jobs.list_recent_jobs(settings))

    with st.container(border=True):
        ui.section_title("الملفات الجاهزة للتحميل")
        latest_job = jobs.get_latest_job(settings)
        ui.render_downloads(latest_job)

    latest_job = jobs.get_latest_job(settings)
    if latest_job and latest_job.get("status") in jobs.RUNNING_STATUSES:
        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()
