from __future__ import annotations

import shutil
import time

import streamlit as st

from src import cost, jobs, storage, tts, ui, worker, youtube
from src.config import LANGUAGES, TTS_STYLES, TTS_VOICES, load_settings
from src.openai_client import get_monthly_spend_usd
from src.logging_utils import configure_logging, log_event


@st.cache_data(ttl=300, show_spinner=False)
def cached_monthly_spend(openai_admin_key: str) -> float | None:
    settings = load_settings()
    if not openai_admin_key:
        return None
    return get_monthly_spend_usd(settings)


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
            help="يمكنك رفع ملف من جهازك أو وضع رابط YouTube لاستخراج الصوت فقط.",
        )
        uploaded_file = None
        youtube_url = ""
        if input_mode == "رفع ملف":
            uploaded_file = st.file_uploader(
                "ارفع ملف فيديو أو صوت",
                type=["mp4", "mov", "mkv", "webm", "mp3", "m4a", "aac", "wav", "flac", "ogg", "opus"],
                help="سيتم استخراج الصوت وضغطه قبل إرساله إلى OpenAI.",
            )
            if uploaded_file and uploaded_file.size > settings.max_upload_mb * 1024 * 1024:
                st.error(f"حجم الملف أكبر من الحد المسموح: {settings.max_upload_mb} MB.")
        else:
            youtube_url = st.text_input(
                "رابط YouTube",
                placeholder="https://www.youtube.com/watch?v=...",
                help="سيستخدم التطبيق yt-dlp لتنزيل الصوت فقط عندما يكون ذلك ممكنًا.",
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
            help="سيتم إنشاء ملف SRT وملف صوت مستقل لكل لغة تختارها.",
        )
        voice = st.selectbox(
            "الصوت",
            options=TTS_VOICES,
            index=TTS_VOICES.index(settings.openai_tts_voice)
            if settings.openai_tts_voice in TTS_VOICES
            else 0,
            help="بعض الأصوات قد تبدو أفضل في الإنجليزية، لكنها تستطيع نطق لغات متعددة.",
        )
        voice_style = st.selectbox(
            "أسلوب الصوت",
            options=list(TTS_STYLES.keys()),
            index=list(TTS_STYLES.keys()).index(settings.tts_naturalness_style),
            format_func=lambda style_code: TTS_STYLES[style_code]["label_ar"],
            help="اختر طريقة الإلقاء المناسبة لنوع الفيديو.",
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
                help="ينشئ عينة قصيرة لكل صوت ويحفظها لإعادة الاستخدام.",
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
            help="MP3 مناسب وسهل الرفع. M4A مناسب أيضًا لـ YouTube.",
        )

    with st.container(border=True):
        ui.section_title("تقدير التكلفة")
        estimated_minutes = st.number_input(
            "مدة تقريبية بالدقائق",
            min_value=0.5,
            max_value=600.0,
            value=20.0,
            step=1.0,
            help="هذا تقدير مبدئي قبل قراءة الملف فعليًا. سيتم تحديثه بعد التفريغ.",
        )
        estimate = cost.estimate_from_minutes(settings, estimated_minutes, len(selected_languages) or 1)
        monthly_spend = cached_monthly_spend(settings.openai_admin_key)
        budget = cost.budget_status(settings, estimate, monthly_spend)
        ui.render_costs(estimate, monthly_spend, budget)

    with st.container(border=True):
        ui.section_title("بدء المعالجة")
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
                    help="يحفظ الملفات المكتملة ثم يوقف العملية بعد الخطوة الحالية.",
                ):
                    jobs.request_cancel(settings, active_job["job_id"])
                    st.warning("سيتم إيقاف العملية بأمان بعد انتهاء الخطوة الحالية.")
                    st.rerun()

        can_start = (
            settings.openai_api_key
            and ffmpeg_ready
            and source_ready
            and file_size_ok
            and bool(selected_languages)
            and not active_job
            and budget.get("allowed", True)
        )
        if st.button(
            "بدء المعالجة",
            type="primary",
            disabled=not can_start,
            help="سيبدأ العمل في الخلفية ويحفظ الحالة في SQLite.",
        ):
            input_type = "upload" if input_mode == "رفع ملف" else "youtube"
            source_name = uploaded_file.name if uploaded_file else youtube_url
            config = {
                "voice": voice,
                "voice_style": voice_style,
                "output_format": output_format,
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
            st.session_state.pop("dismissed_job_id", None)
            st.success("بدأت العملية. يمكنك إغلاق التبويب وسيستمر العمل ما دام تطبيق Streamlit يعمل.")
            st.rerun()

        if (
            latest_job
            and latest_job.get("status") == "interrupted"
            and not active_job
            and dismissed_job_id != latest_job.get("job_id")
        ):
            if st.button(
                "استكمال من آخر خطوة محفوظة",
                help="سيحاول التطبيق تخطي الملفات المكتملة والمتابعة من آخر checkpoint.",
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
                help="يخفي رسالة العملية السابقة دون حذف ملفاتها.",
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

    with st.container(border=True):
        ui.section_title("إحصاءات التكلفة")
        latest_job = jobs.get_latest_job(settings)
        if latest_job:
            stored_estimate = latest_job.get("estimated_cost_json") or estimate
            stored_budget = cost.budget_status(settings, stored_estimate, monthly_spend)
            ui.render_costs(stored_estimate, monthly_spend, stored_budget)
        else:
            ui.render_costs(estimate, monthly_spend, budget)

    latest_job = jobs.get_latest_job(settings)
    if latest_job and latest_job.get("status") in jobs.RUNNING_STATUSES:
        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()
