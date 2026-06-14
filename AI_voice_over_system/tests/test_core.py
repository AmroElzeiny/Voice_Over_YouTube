from __future__ import annotations

import tempfile
import unittest
import io
import json
import base64
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src import cost, jobs, preflight, subtitles, tts as tts_module, worker, youtube
from src.config import LANGUAGES, TTS_STYLES, load_settings
from src.logging_utils import close_logging, log_event
from src.tts import (
    VOICE_SAMPLE_TEXT,
    analyze_voiceover_timeline,
    build_tts_blocks,
    schedule_tts_blocks,
)
from src.youtube import validate_youtube_url, yt_dlp_available
from tools.local_youtube_audio import build_ydl_options
import app as streamlit_app


class CoreBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.settings = replace(
            load_settings(),
            jobs_dir=base / "jobs",
            sqlite_path=base / "jobs" / "jobs.db",
            voice_samples_dir=base / "voice_samples",
            app_log_path=base / "logs" / "app.log",
        )
        jobs.init_db(self.settings)

    def tearDown(self) -> None:
        close_logging(self.settings)
        self.temp_dir.cleanup()

    def test_required_languages_exist(self) -> None:
        self.assertTrue({"ar", "en", "de", "hi", "ms", "id", "zh"}.issubset(LANGUAGES))

    def test_voice_styles_and_preview_sentence(self) -> None:
        self.assertEqual(
            set(TTS_STYLES),
            {"warm_neutral", "educational", "documentary", "energetic", "calm"},
        )
        self.assertEqual(VOICE_SAMPLE_TEXT, "Hello, this is me. How may I help you?")

    def test_youtube_module_and_url_validation(self) -> None:
        self.assertTrue(yt_dlp_available())
        self.assertTrue(validate_youtube_url("https://www.youtube.com/watch?v=abc123"))
        self.assertTrue(validate_youtube_url("https://youtu.be/abc123"))
        self.assertFalse(validate_youtube_url("https://notyoutube.com/watch?v=abc123"))

    def test_cloud_flow_has_no_metadata_probe_or_visitor_browser_detection(self) -> None:
        self.assertFalse(hasattr(youtube, "probe_youtube_duration"))
        self.assertFalse(hasattr(youtube, "browser_user_agent"))
        self.assertFalse(hasattr(streamlit_app, "detect_browser_user_agent"))

    def test_only_explicit_server_user_agent_is_used(self) -> None:
        settings = replace(
            self.settings,
            yt_dlp_user_agent="Configured Agent",
            yt_dlp_cookies_from_browser="chrome",
        )
        with patch("src.youtube._detect_js_runtime", return_value=None):
            args = youtube._common_args(settings, include_cookies=False)
        self.assertEqual(args[args.index("--user-agent") + 1], "Configured Agent")
        self.assertNotIn("--cookies-from-browser", args)

    def test_youtube_auto_runtime_uses_python_deno_binary(self) -> None:
        with (
            patch("src.youtube.shutil.which", return_value=None),
            patch("src.youtube._python_deno_path", return_value="/app/bin/deno"),
        ):
            runtime = youtube._detect_js_runtime(self.settings)
            access_args = youtube._common_args(self.settings, include_cookies=False)

        self.assertEqual(runtime, "deno:/app/bin/deno")
        self.assertEqual(access_args[:2], ["--js-runtimes", "deno:/app/bin/deno"])

    def test_youtube_cookie_secret_is_materialized_and_used(self) -> None:
        cookie_text = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"
        settings = replace(
            self.settings,
            base_dir=Path(self.temp_dir.name),
            yt_dlp_cookies_base64=base64.b64encode(cookie_text.encode()).decode(),
            yt_dlp_user_agent="Mozilla/5.0 Test Browser",
            yt_dlp_proxy="http://user:password@proxy.example:8080",
        )

        with patch("src.youtube._detect_js_runtime", return_value=None):
            args = youtube._common_args(settings, include_cookies=True)

        cookies_path = Path(args[args.index("--cookies") + 1])
        self.assertTrue(cookies_path.exists())
        self.assertEqual(cookies_path.read_text(encoding="utf-8"), cookie_text)
        self.assertIn("Mozilla/5.0 Test Browser", args)
        self.assertIn("http://user:password@proxy.example:8080", args)

    def test_invalid_youtube_cookie_secret_is_rejected(self) -> None:
        settings = replace(self.settings, yt_dlp_cookies_base64="not-base64")
        with (
            patch("src.youtube._detect_js_runtime", return_value=None),
            self.assertRaisesRegex(youtube.YouTubeError, "YT_DLP_COOKIES_BASE64"),
        ):
            youtube._common_args(settings, include_cookies=True)

    def test_youtube_403_has_specific_message(self) -> None:
        event, message = youtube._classify_failure(
            "[jsc:deno] Solving JS challenges using deno\n"
            "fragment not found; Skipping fragment 22\n"
            "ERROR: The downloaded file is empty"
        )
        self.assertEqual(event, youtube.YOUTUBE_MEDIA_403)
        self.assertIn("رفع ملف الصوت", message)

        login_event, login_message = youtube._classify_failure(
            "web_safari player response playability status: LOGIN_REQUIRED"
        )
        self.assertEqual(login_event, youtube.YOUTUBE_IP_REPUTATION_BLOCK)
        self.assertIn("خادم Streamlit", login_message)

    def test_po_message_does_not_claim_an_attempt_without_a_provider(self) -> None:
        event, message = youtube._classify_failure(
            "PO token required for this format",
            provider_ready=False,
        )
        self.assertEqual(event, youtube.YOUTUBE_POT_PROVIDER_MISSING)
        self.assertNotIn("بعد PO Token", message)

    def test_missing_javascript_runtime_has_specific_message(self) -> None:
        event, _message = youtube._classify_failure(
            "No supported JavaScript runtime could be found"
        )
        self.assertEqual(event, youtube.YOUTUBE_EJS_MISSING)

    def test_youtube_download_uses_one_anonymous_strategy(self) -> None:
        source_dir = Path(self.temp_dir.name) / "job" / "source"
        log_path = Path(self.temp_dir.name) / "job" / "youtube.log"
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(args)
            source_dir.mkdir(parents=True, exist_ok=True)
            (source_dir / "youtube_source.mp3").write_bytes(b"x" * 2048)
            return type("Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

        with (
            patch("src.youtube.subprocess.run", side_effect=fake_run),
            patch("src.youtube._common_args", return_value=[]),
            patch("src.youtube.collect_diagnostics", return_value={"ejs_available": True, "yt_dlp_version": "test", "deno_version": "test", "impersonation_available": False, "proxy_configured": False}),
            patch("src.youtube._provider_configuration", return_value={"configured": False, "plugin_detected": False, "ready": False}),
            patch("src.youtube._download_external", return_value=None),
            patch("src.youtube.media.probe_duration", return_value=12.0),
        ):
            output = youtube.download_youtube_audio(
                "https://youtu.be/abc123",
                source_dir,
                self.settings,
                log_path,
            )

        self.assertEqual(output.name, "youtube_source.mp3")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--extractor-args", calls[0])
        self.assertIn("--no-plugin-dirs", calls[0])

    def test_strategies_are_configured_and_capped_at_three(self) -> None:
        settings = replace(
            self.settings,
            yt_dlp_cookies_base64="configured",
            yt_dlp_cloud_direct_enabled=True,
        )
        provider = {
            "ready": True,
            "extractor_arg": "youtubepot-bgutilhttp:base_url=https://provider.example",
        }
        with patch("src.youtube._provider_configuration", return_value=provider):
            strategies = youtube._strategies(settings)
        self.assertLessEqual(len(strategies), 3)
        self.assertEqual(
            [item["name"] for item in strategies],
            ["configured_po_provider", "configured_cookies", "anonymous_cloud"],
        )
        self.assertTrue(strategies[0]["allow_plugins"])
        self.assertFalse(strategies[1]["allow_plugins"])
        self.assertFalse(strategies[2]["allow_plugins"])

        with patch(
            "src.youtube._provider_configuration",
            return_value={"ready": False, "extractor_arg": ""},
        ):
            without_provider = youtube._strategies(settings)
        self.assertNotIn(
            "configured_po_provider",
            [item["name"] for item in without_provider],
        )

    def test_authentication_failure_is_not_repeated_for_anonymous_mode(self) -> None:
        calls = []

        def fake_run(args, **_kwargs):
            calls.append(args)
            return type(
                "Completed",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "web_safari player response playability status: LOGIN_REQUIRED",
                },
            )()

        with (
            patch("src.youtube.subprocess.run", side_effect=fake_run),
            patch("src.youtube._common_args", return_value=[]),
            patch("src.youtube.collect_diagnostics", return_value={"ejs_available": True, "yt_dlp_version": "test", "deno_version": "test", "impersonation_available": False, "proxy_configured": False}),
            patch("src.youtube._provider_configuration", return_value={"configured": False, "plugin_detected": False, "ready": False}),
            patch("src.youtube._download_external", return_value=None),
        ):
            with self.assertRaises(youtube.YouTubeError) as raised:
                youtube.download_youtube_audio(
                    "https://youtu.be/abc123",
                    Path(self.temp_dir.name) / "auth" / "source",
                    self.settings,
                )
        self.assertEqual(len(calls), 1)
        self.assertEqual(raised.exception.failure_type, youtube.YOUTUBE_IP_REPUTATION_BLOCK)
        self.assertTrue(raised.exception.needs_local_audio)

    def test_download_never_exceeds_three_distinct_attempts(self) -> None:
        calls = []
        strategies = [
            {
                "name": f"strategy-{index}",
                "player_client": "default",
                "include_cookies": False,
                "extra_args": [],
            }
            for index in range(4)
        ]

        def fake_run(args, **_kwargs):
            calls.append(args)
            return type(
                "Completed",
                (),
                {"returncode": 1, "stdout": "", "stderr": "network timeout"},
            )()

        with (
            patch("src.youtube.subprocess.run", side_effect=fake_run),
            patch("src.youtube._strategies", return_value=strategies),
            patch("src.youtube._common_args", return_value=[]),
            patch("src.youtube.collect_diagnostics", return_value={"ejs_available": True, "yt_dlp_version": "test", "deno_version": "test", "impersonation_available": False, "proxy_configured": False}),
            patch("src.youtube._provider_configuration", return_value={"configured": False, "plugin_detected": False, "ready": False}),
            patch("src.youtube._download_external", return_value=None),
            self.assertRaises(youtube.YouTubeError),
        ):
            youtube.download_youtube_audio(
                "https://youtu.be/abc123",
                Path(self.temp_dir.name) / "attempts" / "source",
                self.settings,
            )
        self.assertEqual(len(calls), 3)

    def test_youtube_worker_does_not_forward_browser_identity(self) -> None:
        job = {
            "job_id": "youtube-job",
            "input_type": "youtube",
            "source_name_or_url": "https://youtu.be/abc123",
            "config_json": {},
        }
        expected = Path(self.temp_dir.name) / "downloaded.mp3"
        with patch("src.worker.youtube.download_youtube_audio", return_value=expected) as download:
            result = worker._load_source(
                self.settings,
                job,
                Path(self.temp_dir.name),
                Path(self.temp_dir.name) / "job.log",
            )

        self.assertEqual(result, expected)
        self.assertEqual(len(download.call_args.args), 4)
        self.assertEqual(download.call_args.kwargs, {})

    def test_local_audio_pause_can_resume_the_same_job(self) -> None:
        job_id = jobs.create_job(
            self.settings,
            "youtube",
            "https://youtu.be/abc123",
            ["ar"],
            {},
            {},
        )
        jobs.wait_for_local_audio(self.settings, job_id, "blocked")
        self.assertEqual(jobs.get_job(self.settings, job_id)["status"], "needs_local_audio")
        jobs.update_job(
            self.settings,
            job_id,
            input_type="upload",
            source_name_or_url="audio.mp3",
            status="queued",
            config_json={"source_file": "audio.mp3", "original_youtube_url": "https://youtu.be/abc123"},
        )
        resumed = jobs.get_job(self.settings, job_id)
        self.assertEqual(resumed["job_id"], job_id)
        self.assertEqual(resumed["input_type"], "upload")
        source_folder = self.settings.jobs_dir / job_id / "source"
        source_folder.mkdir(parents=True, exist_ok=True)
        expected_source = source_folder / "audio.mp3"
        expected_source.write_bytes(b"local audio")
        with patch("src.worker.youtube.download_youtube_audio") as download:
            loaded = worker._load_source(
                self.settings,
                resumed,
                self.settings.jobs_dir / job_id,
                self.settings.jobs_dir / job_id / "logs" / "job.log",
            )
        self.assertEqual(loaded, expected_source)
        download.assert_not_called()
        jobs.wait_for_local_audio(self.settings, job_id, "blocked again")
        jobs.cancel_job(self.settings, job_id)
        self.assertEqual(jobs.get_job(self.settings, job_id)["status"], "cancelled")

    def test_local_helper_uses_browser_cookies_only_when_requested(self) -> None:
        plain = build_ydl_options(Path(self.temp_dir.name))
        browser = build_ydl_options(Path(self.temp_dir.name), "chrome")
        self.assertNotIn("cookiesfrombrowser", plain)
        self.assertEqual(browser["cookiesfrombrowser"], ("chrome",))
        self.assertEqual(browser["postprocessors"][0]["preferredcodec"], "mp3")

    def test_external_downloader_accepts_audio_bytes(self) -> None:
        response = type(
            "Response",
            (),
            {
                "content": b"x" * 2048,
                "headers": {"Content-Type": "audio/mpeg"},
                "raise_for_status": lambda self: None,
            },
        )()
        settings = replace(
            self.settings,
            youtube_external_downloader_url="https://downloader.example/audio",
            youtube_external_downloader_token="secret-token",
        )
        output_dir = Path(self.temp_dir.name) / "external"
        output_dir.mkdir()
        with (
            patch("src.youtube.requests.post", return_value=response) as post,
            patch("src.youtube.media.probe_duration", return_value=5.0),
        ):
            output = youtube._download_external(
                "https://youtu.be/abc123",
                "job-1",
                output_dir,
                settings,
            )
        self.assertEqual(output.read_bytes(), b"x" * 2048)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer secret-token")

    def test_uploaded_duration_uses_ffprobe_and_restores_stream(self) -> None:
        uploaded = io.BytesIO(b"fake media")
        uploaded.seek(3)
        with patch("src.preflight.media.probe_duration", return_value=42.0):
            duration = preflight.probe_uploaded_duration(uploaded, "video.mp4")
        self.assertEqual(duration, 42.0)
        self.assertEqual(uploaded.tell(), 3)

    def test_duration_estimate_uses_configured_tts_cost_per_minute(self) -> None:
        estimate = cost.estimate_from_minutes(self.settings, 10, 2)
        expected_audio_cost = 10 * 2 * self.settings.openai_tts_estimated_usd_per_min
        self.assertAlmostEqual(estimate["tts_audio_usd_estimated"], expected_audio_cost)

    def test_recorded_jobs_summary_uses_tokens_and_saved_costs(self) -> None:
        summary = cost.recorded_jobs_summary(
            [
                {
                    "actual_cost_json": {
                        "total_usd": 0.25,
                        "total_billable_tokens": 1200,
                        "transcription_minutes": 3.5,
                    }
                },
                {
                    "status": "completed",
                    "actual_cost_json": {},
                    "estimated_cost_json": {
                        "transcription_usd": 0.01,
                        "translation_usd": 0.02,
                        "tts_usd": 0.03,
                    },
                },
            ],
            {"cost_usd": 0.04, "total_tokens": 80},
        )
        self.assertAlmostEqual(summary["total_usd"], 0.35)
        self.assertEqual(summary["total_billable_tokens"], 1280)
        self.assertEqual(summary["job_count"], 2)

    def test_hardcoded_balance_deducts_recorded_usage(self) -> None:
        settings = replace(self.settings, openai_manual_available_balance_usd=10.0)
        self.assertAlmostEqual(cost.supposed_balance(settings, 2.25), 7.75)

        status = cost.budget_status(
            settings,
            {"total_usd": 8.0},
            None,
            recorded_cost_usd=2.25,
        )
        self.assertFalse(status["allowed"])
        self.assertAlmostEqual(status["available_usd"], 7.75)

    def test_translation_cost_is_not_doubled_when_job_resumes(self) -> None:
        job_id = jobs.create_job(
            self.settings,
            input_type="upload",
            source_name_or_url="source.mp3",
            selected_languages=["en"],
            estimated_cost={},
            config={},
        )
        usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        worker._update_actual_translation_cost(self.settings, job_id, "en", usage)
        worker._update_actual_translation_cost(self.settings, job_id, "en", usage)
        actual = jobs.get_job(self.settings, job_id)["actual_cost_json"]
        self.assertEqual(actual["translation_total_tokens"], 150)

    def test_tts_usage_manifest_counts_each_generated_part_once(self) -> None:
        from pydub import AudioSegment

        tts_dir = Path(self.temp_dir.name) / "tts"
        usage_path = Path(self.temp_dir.name) / "tts_usage_en.json"
        manifest = {"model": self.settings.openai_tts_model, "language": "en", "parts": {}}
        updates: list[dict] = []

        def fake_speech(_client, _model, _voice, _text, _instructions, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"audio")

        with (
            patch("src.tts._speech_create", side_effect=fake_speech),
            patch("src.tts.AudioSegment.from_file", return_value=AudioSegment.silent(duration=1000)),
        ):
            for _ in range(2):
                tts_module._render_block_audio(
                    object(),
                    self.settings,
                    "coral",
                    "Hello world",
                    "Speak clearly",
                    1,
                    tts_dir,
                    "normal",
                    usage_path,
                    manifest,
                    updates.append,
                    None,
                )

        self.assertEqual(len(manifest["parts"]), 1)
        self.assertEqual(manifest["output_audio_tokens"], 20)
        self.assertGreater(manifest["input_tokens"], 0)
        self.assertGreater(manifest["cost_usd"], 0)
        self.assertEqual(len(updates), 1)

    def test_voice_sample_usage_is_persisted_once(self) -> None:
        from pydub import AudioSegment

        def fake_speech(_client, _model, _voice, _text, _instructions, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"audio")

        with (
            patch("src.tts.get_client", return_value=object()),
            patch("src.tts._speech_create", side_effect=fake_speech) as speech,
            patch("src.tts.AudioSegment.from_file", return_value=AudioSegment.silent(duration=1000)),
        ):
            for _ in range(2):
                tts_module.generate_voice_sample(
                    self.settings,
                    "coral",
                    VOICE_SAMPLE_TEXT,
                    self.settings.voice_samples_dir,
                )

        usage = json.loads(
            tts_module.voice_samples_usage_path(self.settings.voice_samples_dir).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(speech.call_count, 1)
        self.assertEqual(len(usage["parts"]), 1)
        self.assertEqual(usage["output_audio_tokens"], 20)
        self.assertGreater(usage["cost_usd"], 0)

    def test_logging_redacts_api_keys(self) -> None:
        secret = "configured-test-secret-value-1234567890"
        cookie_secret = "cookie-secret-value-abcdefghijklmnopqrstuvwxyz"
        proxy_secret = "http://user:password@proxy.example:8080"
        external_token = "external-downloader-token-1234567890"
        settings = replace(
            self.settings,
            openai_api_key=secret,
            yt_dlp_cookies_base64=cookie_secret,
            yt_dlp_proxy=proxy_secret,
            youtube_external_downloader_token=external_token,
        )
        log_event(
            settings,
            "redaction_test",
            f"Key value: {secret}; cookies: {cookie_secret}; proxy: {proxy_secret}; external: {external_token}",
        )
        content = settings.app_log_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, content)
        self.assertNotIn(cookie_secret, content)
        self.assertNotIn(proxy_secret, content)
        self.assertNotIn(external_token, content)
        self.assertIn("REDACTED_KEY", content)
        self.assertIn("REDACTED_SECRET", content)
        close_logging(settings)

    def test_tts_block_grouping_preserves_segment_ids(self) -> None:
        segments = [
            {"id": 1, "start": 0.0, "end": 1.0, "target_text": "First line"},
            {"id": 2, "start": 1.2, "end": 2.2, "target_text": "Second line"},
            {"id": 3, "start": 4.5, "end": 5.5, "target_text": "Third line"},
        ]
        blocks = build_tts_blocks(segments, self.settings)
        preserved_ids = [segment_id for block in blocks for segment_id in block["segment_ids"]]
        self.assertEqual(preserved_ids, [1, 2, 3])

    def test_scheduler_never_overlaps_single_speaker(self) -> None:
        blocks = [
            {"block_id": 1, "segment_ids": [1], "start": 0.0, "end": 1.0, "target_duration_ms": 1000},
            {"block_id": 2, "segment_ids": [2], "start": 0.8, "end": 1.8, "target_duration_ms": 1000},
            {"block_id": 3, "segment_ids": [3], "start": 2.0, "end": 3.0, "target_duration_ms": 1000},
        ]
        placements = schedule_tts_blocks(blocks, [1500, 1200, 800], self.settings)
        for previous, current in zip(placements, placements[1:]):
            self.assertGreaterEqual(current["start_ms"], previous["end_ms"])

    def test_voiceover_qa_schema_passes_for_clean_timeline(self) -> None:
        from pydub import AudioSegment

        blocks = [
            {"block_id": 1, "segment_ids": [1], "start": 0.0, "end": 1.0, "text": "One"},
            {"block_id": 2, "segment_ids": [2], "start": 1.2, "end": 2.2, "text": "Two"},
        ]
        placements = [
            {"block_id": 1, "start_ms": 0, "end_ms": 1000, "trimmed": False, "regenerated": False},
            {"block_id": 2, "start_ms": 1200, "end_ms": 2200, "trimmed": False, "regenerated": False},
        ]
        report = analyze_voiceover_timeline(
            blocks,
            placements,
            AudioSegment.silent(duration=2200),
            self.settings,
        )
        expected_keys = {
            "total_duration_seconds",
            "original_duration_seconds",
            "duration_delta_seconds",
            "overlap_count",
            "max_overlap_seconds",
            "long_silence_count",
            "max_silence_seconds",
            "trimmed_block_count",
            "regenerated_block_count",
            "empty_text_blocks",
            "warnings",
            "passed",
        }
        self.assertTrue(expected_keys.issubset(report))
        self.assertTrue(report["passed"])
        self.assertEqual(report["overlap_count"], 0)

    def test_srt_output_is_valid_utf8(self) -> None:
        path = Path(self.temp_dir.name) / "test.srt"
        subtitles.write_srt(
            path,
            [{"id": 1, "start": 0.0, "end": 1.2, "target_text": "مرحبا بالعالم"}],
        )
        content = path.read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 --> 00:00:01,200", content)
        self.assertIn("مرحبا بالعالم", content)

    def test_cancelled_job_cannot_become_failed(self) -> None:
        job_id = jobs.create_job(
            self.settings,
            input_type="youtube",
            source_name_or_url="https://youtu.be/example",
            selected_languages=["en"],
            estimated_cost={},
            config={},
        )
        jobs.request_cancel(self.settings, job_id)
        worker.run_job(self.settings, job_id)
        self.assertEqual(jobs.get_job(self.settings, job_id)["status"], "cancelled")
        jobs.fail_job(self.settings, job_id, "should not replace cancellation")
        self.assertEqual(jobs.get_job(self.settings, job_id)["status"], "cancelled")

    def test_worker_pauses_cloud_block_instead_of_failing(self) -> None:
        job_id = jobs.create_job(
            self.settings,
            input_type="youtube",
            source_name_or_url="https://youtu.be/example",
            selected_languages=["en"],
            estimated_cost={},
            config={},
        )
        error = youtube.YouTubeError(
            "ارفع ملف الصوت",
            youtube.YOUTUBE_MEDIA_403,
            needs_local_audio=True,
        )
        with patch("src.worker._load_source", side_effect=error):
            worker.run_job(self.settings, job_id)
        saved = jobs.get_job(self.settings, job_id)
        self.assertEqual(saved["status"], "needs_local_audio")
        self.assertEqual(saved["progress_percent"], 3)

    def test_language_pipeline_logs_after_each_stage_completes(self) -> None:
        source = Path(self.temp_dir.name) / "source.mp3"
        source.write_bytes(b"source")
        event_names: list[str] = []

        job_id = jobs.create_job(
            self.settings,
            input_type="upload",
            source_name_or_url="source.mp3",
            selected_languages=["en"],
            estimated_cost={},
            config={"voice": "coral", "output_format": "mp3"},
        )

        def fake_extract(_source, output, _settings, _log):
            output.write_bytes(b"normalized")
            return output

        def fake_translate(_settings, job_path, segments, *_args, **_kwargs):
            translated = [{**segment, "target_text": "Translated"} for segment in segments]
            (job_path / "translation_en.srt").write_text("translated", encoding="utf-8")
            return translated, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

        def fake_voiceover(_settings, job_path, *_args, **_kwargs):
            output = job_path / "voiceover_en.mp3"
            output.write_bytes(b"voice")
            return output

        def capture_event(_settings, event, _message, **_kwargs):
            event_names.append(event)

        with (
            patch("src.worker._load_source", return_value=source),
            patch("src.worker.media.ffprobe_json", return_value={"format": {"duration": "10"}}),
            patch("src.worker.media.extract_or_normalize_audio", side_effect=fake_extract),
            patch("src.worker.media.probe_duration", return_value=10.0),
            patch("src.worker.cost.budget_status", return_value={"allowed": True, "available_usd": None, "source": "none"}),
            patch(
                "src.worker.chunking.create_audio_chunks",
                return_value=[{"filename": "chunk_000.mp3", "size_bytes": 100}],
            ),
            patch(
                "src.worker.transcription.transcribe_chunks",
                return_value=[
                    {"id": 1, "start": 0.0, "end": 1.0, "source_text": "Source", "target_text": None}
                ],
            ),
            patch("src.worker.translation.translate_segments", side_effect=fake_translate),
            patch("src.worker.tts.generate_voiceover", side_effect=fake_voiceover),
            patch("src.worker.log_event", side_effect=capture_event),
        ):
            worker.run_job(self.settings, job_id)

        self.assertEqual(jobs.get_job(self.settings, job_id)["status"], "completed")
        self.assertLess(event_names.index("translation_completed"), event_names.index("voiceover_completed"))
        self.assertLess(event_names.index("voiceover_completed"), event_names.index("job_completed"))

    def test_repository_hygiene_patterns_are_present(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        gitignore = (project_root / ".gitignore").read_text(encoding="utf-8")
        for required in (
            ".env",
            "data/jobs/",
            "data/voice_samples/",
            "data/private/",
            "__pycache__/",
            ".streamlit/secrets.toml",
        ):
            self.assertIn(required, gitignore)

        env_example = (project_root / ".env.example").read_text(encoding="utf-8")
        self.assertIn("OPENAI_API_KEY=", env_example)
        self.assertNotRegex(env_example, r"sk-[A-Za-z0-9_-]{20,}")

        requirements = (project_root / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("pydub-ng==0.2.0", requirements)
        self.assertNotRegex(requirements, r"(?m)^pydub\s*$")
        self.assertIn('audioop-lts; python_version >= "3.13"', requirements)
        self.assertIn("deno==2.8.3", requirements)
        self.assertIn("yt-dlp[default,curl-cffi]>=2026.6.9", requirements)
        self.assertNotIn("yt-dlp-getpot-wpc", requirements)

        repository_root = project_root.parent
        system_packages = (repository_root / "packages.txt").read_text(encoding="utf-8")
        self.assertIn("ffmpeg", system_packages)
        self.assertNotIn("chromium", system_packages)
        self.assertNotIn("xvfb", system_packages)
        self.assertNotIn("xauth", system_packages)
        self.assertTrue((repository_root / ".streamlit" / "config.toml").exists())

        app_source = (project_root / "app.py").read_text(encoding="utf-8")
        self.assertNotIn('ui.section_title("تقدير التكلفة")', app_source)
        self.assertNotIn('ui.section_title("إحصاءات التكلفة")', app_source)
        self.assertIn('"متابعة وبدء العمل"', app_source)
        self.assertIn('"بدء عملية جديدة"', app_source)
        self.assertIn('"إلغاء العملية"', app_source)
        self.assertNotIn("إنفاق هذا الشهر", app_source)
        self.assertIn('columns[1].metric("تكلفة كل الملفات"', app_source)
        self.assertIn('columns[3].metric("الرصيد المحسوب"', app_source)
        self.assertNotIn("st.context.headers", app_source)
        self.assertNotIn("browser_user_agent", app_source)
        self.assertNotIn("probe_youtube_duration", app_source)


if __name__ == "__main__":
    unittest.main()
