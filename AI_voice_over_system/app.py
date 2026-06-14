from __future__ import annotations

import shutil
import time

import streamlit as st

from src import cost, jobs, preflight, storage, tts, ui, worker, youtube
from src.build_info import APP_BUILD_ID
from src.config import LANGUAGES, TTS_STYLES, TTS_VOICES, load_settings
from src.logging_utils import configure_logging, log_event


def account_usage_summary(settings) -> dict:
    voice_sample_usage = storage.read_json(
        tts.voice_samples_usage_path(settings.voice_samples_dir),
        {},
    )
    return cost.recorded_jobs_summary(
        jobs.list_all_jobs(settings),
        voice_sample_usage,
    )


def render_openai_account_status(settings) -> None:
    summary = account_usage_summary(settings)
    remaining_balance = cost.supposed_balance(settings, summary["total_usd"])
    with st.expander("حالة حساب OpenAI"):
        columns = st.columns(4)
        if settings.openai_manual_available_balance_usd is not None:
            columns[0].metric("الرصيد الأولي", ui.money(settings.openai_manual_available_balance_usd))
        else:
            columns[0].metric("الرصيد الأولي", "غير مضبوط")
        columns[1].metric("تكلفة كل الملفات", ui.money(summary["total_usd"]))
        columns[2].metric("الرموز المحسوبة", f"{int(summary['total_billable_tokens']):,}")
        if remaining_balance is not None:
            columns[3].metric("الرصيد المحسوب", ui.money(remaining_balance))
        else:
            columns[3].metric("الرصيد المحسوب", "غير متاح")
        st.caption(f"عدد العمليات المحسوبة: {int(summary['job_count'])}")


def render_youtube_diagnostics(settings) -> None:
    diagnostics = youtube.collect_diagnostics(settings)
    with st.expander("تشخيص اتصال YouTube"):
        rows = {
            "yt-dlp": "متاح" if diagnostics.get("yt_dlp_available") else "غير متاح",
            "إصدار yt-dlp": diagnostics.get("yt_dlp_version") or "غير متاح",
            "دعم EJS": "جاهز" if diagnostics.get("ejs_available") else "غير جاهز",
            "Deno": diagnostics.get("deno_version") or "غير متاح",
            "تقليد المتصفح": "متاح" if diagnostics.get("impersonation_available") else "غير متاح",
            "ملف تسجيل الدخول": "مضبوط" if diagnostics.get("cookies_configured") else "غير مضبوط",
            "الخادم الوسيط": "مضبوط" if diagnostics.get("proxy_configured") else "غير مضبوط",
            "إعداد مزود PO Token": "مضبوط" if diagnostics.get("pot_provider_configured") else "غير مضبوط",
            "إضافة مزود PO Token": "مكتشفة" if diagnostics.get("pot_provider_detected") else "غير مكتشفة",
            "مزود PO Token": "جاهز" if diagnostics.get("pot_provider_ready") else "غير جاهز",
            "التنزيل المباشر": "مفعّل" if diagnostics.get("cloud_direct_enabled") else "متوقف",
            "عامل تنزيل خارجي": "مضبوط" if diagnostics.get("external_downloader_configured") else "غير مضبوط",
        }
        for label, value in rows.items():
            st.write(f"**{label}:** {value}")
        st.write(f"**إصدار التشغيل:** {APP_BUILD_ID}")
        st.caption("هذه المعلومات لا تعرض المفاتيح أو محتوى ملف تسجيل الدخول.")


def main() -> None:
    settings = load_settings()
    storage.ensure_storage(settings)
    configure_logging(settings)
    jobs.startup_recovery(settings)
    ui.setup_page(settings)
    log_event(settings, "app_rendered", "Streamlit app rendered.", build_id=APP_BUILD_ID)
    youtube.log_startup_diagnostics(settings)

    st.title(settings.app_title)
    st.markdown(
        '<div class="disclosure">الصوت الناتج تم إنشاؤه بالذكاء الاصطناعي.</div>',
        unsafe_allow_html=True,
    )
    st.caption(f"إصدار التشغيل: {APP_BUILD_ID}")

    latest_job = jobs.get_latest_job(settings)
    active_job = jobs.get_active_job(settings)
    waiting_job = latest_job if latest_job and latest_job.get("status") in jobs.WAITING_STATUSES else None
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
        approximate_minutes = 10.0
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
                help="ألصق رابط فيديو YouTube العام هنا.",
            )
            approximate_minutes = st.number_input(
                "المدة التقريبية بالدقائق",
                min_value=1.0,
                max_value=720.0,
                value=10.0,
                step=1.0,
                help="نستخدمها لحساب أولي فقط. بعد تنزيل الصوت نحسب المدة الحقيقية قبل استخدام OpenAI.",
            )
            st.info("استخدم فقط الفيديوهات التي تملكها أو لديك إذن واضح لمعالجتها.")
            if settings.yt_dlp_cookies_file or settings.yt_dlp_cookies_base64:
                st.caption("تم إعداد ملف تعريف ارتباط YouTube")
                st.warning(
                    "قد تتوقف ملفات الارتباط عن العمل أو يطلب YouTube التحقق مرة أخرى. "
                    "استخدم حسابًا مخصصًا بحذر ولا تستخدم حسابك الرئيسي."
                )
            else:
                st.caption("لم يتم إعداد ملف تعريف ارتباط YouTube")
                st.caption("التنزيل المباشر من خوادم مجانية قد يرفضه YouTube.")
            if settings.yt_dlp_cloud_direct_enabled:
                st.info("سيحاول التطبيق قراءة الصوت من YouTube مباشرة.")
            else:
                st.info("التنزيل المباشر متوقف. يمكنك استخراج الصوت على جهازك مجانًا ثم رفعه هنا.")
            render_youtube_diagnostics(settings)

        source_signature = (
            input_mode,
            uploaded_file.name if uploaded_file else "",
            uploaded_file.size if uploaded_file else 0,
            youtube_url.strip(),
            approximate_minutes if input_mode == "رابط YouTube" else 0,
        )
        previous_signature = st.session_state.get("source_signature")
        if (
            previous_signature is not None
            and previous_signature != source_signature
            and latest_job
            and latest_job.get("status") in {"completed", "failed", "interrupted", "cancelled"}
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

        if waiting_job and waiting_job.get("status") == "needs_local_audio":
            st.warning(
                "تعذر على خادم Streamlit قراءة الصوت مباشرة من YouTube. يمكنك استخراج الصوت "
                "على جهازك ورفعه هنا، وبعدها ستستكمل العملية تلقائيًا."
            )
            waiting_config = dict(waiting_job.get("config_json") or {})
            original_url = str(
                waiting_config.get("original_youtube_url")
                or waiting_job.get("source_name_or_url")
                or ""
            )
            if youtube.validate_youtube_url(original_url):
                with st.expander("طريقة استخراج الصوت على جهازك"):
                    st.caption("شغّل هذا الأمر داخل مجلد المشروع. يمكنك إضافة --browser chrome إذا طلب YouTube تسجيل الدخول.")
                    st.code(
                        f'python tools/local_youtube_audio.py "{original_url}"',
                        language="powershell",
                    )
            fallback_audio = st.file_uploader(
                "ارفع ملف الصوت لهذه العملية",
                type=["mp3", "m4a", "wav", "opus", "webm", "aac", "ogg", "flac"],
                key=f"fallback-audio-{waiting_job['job_id']}",
                help="اختر ملف الصوت الذي نزلته من الفيديو نفسه.",
            )
            fallback_too_large = bool(
                fallback_audio
                and fallback_audio.size > settings.max_upload_mb * 1024 * 1024
            )
            if fallback_too_large:
                st.error(f"حجم الملف أكبر من الحد المسموح: {settings.max_upload_mb} MB.")
            if st.button(
                "رفع الصوت واستكمال العملية",
                type="primary",
                disabled=not fallback_audio or fallback_too_large,
                help="يفحص الملف ثم يكمل نفس العملية دون إنشاء عملية جديدة.",
            ):
                source_folder = storage.source_dir(settings, waiting_job["job_id"])
                source_folder.mkdir(parents=True, exist_ok=True)
                for stale_file in source_folder.glob("youtube_source.*"):
                    stale_file.unlink(missing_ok=True)
                safe_name = storage.safe_filename(fallback_audio.name)
                fallback_audio.seek(0)
                storage.save_uploaded_file(fallback_audio, source_folder / safe_name)
                waiting_config.update(
                    {
                        "source_file": safe_name,
                        "original_youtube_url": original_url,
                        "local_audio_uploaded": True,
                    }
                )
                jobs.update_job(
                    settings,
                    waiting_job["job_id"],
                    input_type="upload",
                    source_name_or_url=safe_name,
                    status="queued",
                    current_step="فحص ملف الصوت المرفوع",
                    error_message=None,
                    config_json=waiting_config,
                )
                worker.start_job_worker(settings, waiting_job["job_id"])
                st.success("تم رفع الصوت. ستستكمل العملية الآن.")
                st.rerun()

        if waiting_job and waiting_job.get("status") == "needs_budget":
            st.warning("توقفت العملية قبل استخدام OpenAI لأن الرصيد المحسوب غير كافٍ.")
            if st.button(
                "إعادة فحص الرصيد واستكمال العملية",
                help="استخدمه بعد تعديل الرصيد المبدئي أو إعدادات اللغات.",
            ):
                jobs.update_job(
                    settings,
                    waiting_job["job_id"],
                    status="queued",
                    current_step="إعادة فحص الرصيد",
                    error_message=None,
                )
                worker.start_job_worker(settings, waiting_job["job_id"])
                st.rerun()

        can_prepare = (
            settings.openai_api_key
            and ffmpeg_ready
            and source_ready
            and file_size_ok
            and bool(selected_languages)
            and not active_job
            and not waiting_job
        )
        if not pending_confirmation:
            if st.button(
                "بدء العملية",
                type="primary",
                disabled=not can_prepare,
                help="يعرض التكلفة المتوقعة قبل بدء العمل.",
            ):
                try:
                    with st.spinner("جاري حساب التكلفة المتوقعة..."):
                        if uploaded_file:
                            duration_seconds = preflight.probe_uploaded_duration(
                                uploaded_file,
                                uploaded_file.name,
                            )
                            provisional = False
                        else:
                            duration_seconds = float(approximate_minutes) * 60
                            provisional = True
                        estimate = cost.estimate_from_minutes(
                            settings,
                            duration_seconds / 60,
                            len(selected_languages),
                        )
                        budget = cost.budget_status(
                            settings,
                            estimate,
                            None,
                            recorded_cost_usd=account_usage_summary(settings)["total_usd"],
                        )
                        if provisional:
                            budget = {
                                **budget,
                                "allowed": True,
                                "provisional_only": True,
                            }
                    pending_confirmation = {
                        "signature": confirmation_signature,
                        "duration_seconds": duration_seconds,
                        "estimate": estimate,
                        "budget": budget,
                        "provisional": provisional,
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
                f"{'المدة التقريبية' if pending_confirmation.get('provisional') else 'مدة الملف'}: "
                f"{duration_minutes:.1f} دقيقة\n\n"
                f"**التكلفة المتوقعة: {ui.money(estimate.get('total_usd'))}**"
            )
            if pending_confirmation.get("provisional"):
                st.caption("بعد تنزيل الصوت سنحسب المدة الحقيقية ونتأكد من الرصيد قبل إرسال أي طلب إلى OpenAI.")
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
                    "duration_is_provisional": bool(pending_confirmation.get("provisional")),
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
            and latest_job.get("status") not in jobs.WAITING_STATUSES
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
