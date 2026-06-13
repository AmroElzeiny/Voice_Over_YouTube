from __future__ import annotations

import tempfile
import unittest
import io
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src import cost, jobs, preflight, subtitles, worker, youtube
from src.config import LANGUAGES, TTS_STYLES, load_settings
from src.logging_utils import close_logging, log_event
from src.openai_client import get_monthly_spend_status
from src.tts import (
    VOICE_SAMPLE_TEXT,
    analyze_voiceover_timeline,
    build_tts_blocks,
    schedule_tts_blocks,
)
from src.youtube import validate_youtube_url, yt_dlp_available


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

    def test_youtube_duration_uses_metadata_without_download(self) -> None:
        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": json.dumps({"duration": 125.5}), "stderr": ""},
        )()
        with patch("src.youtube.subprocess.run", return_value=completed) as run:
            duration = youtube.probe_youtube_duration("https://youtu.be/abc123", self.settings)
        self.assertEqual(duration, 125.5)
        command = run.call_args.args[0]
        self.assertIn("--skip-download", command)
        self.assertIn("--dump-single-json", command)

    def test_uploaded_duration_uses_ffprobe_and_restores_stream(self) -> None:
        uploaded = io.BytesIO(b"fake media")
        uploaded.seek(3)
        with patch("src.preflight.media.probe_duration", return_value=42.0):
            duration = preflight.probe_uploaded_duration(uploaded, "video.mp4")
        self.assertEqual(duration, 42.0)
        self.assertEqual(uploaded.tell(), 3)

    def test_monthly_spend_explains_missing_admin_key(self) -> None:
        status = get_monthly_spend_status(replace(self.settings, openai_admin_key=""))
        self.assertEqual(status["status"], "admin_key_missing")
        self.assertIsNone(status["amount_usd"])

    def test_duration_estimate_uses_configured_tts_cost_per_minute(self) -> None:
        estimate = cost.estimate_from_minutes(self.settings, 10, 2)
        expected_audio_cost = 10 * 2 * self.settings.openai_tts_estimated_usd_per_min
        self.assertAlmostEqual(estimate["tts_audio_usd_estimated"], expected_audio_cost)

    def test_logging_redacts_api_keys(self) -> None:
        secret = "configured-test-secret-value-1234567890"
        settings = replace(
            self.settings,
            openai_api_key=secret,
        )
        log_event(settings, "redaction_test", f"Key value: {secret}")
        content = settings.app_log_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, content)
        self.assertIn("REDACTED_KEY", content)
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
            patch("src.worker.media.extract_or_normalize_audio", side_effect=fake_extract),
            patch("src.worker.media.probe_duration", return_value=10.0),
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
            "__pycache__/",
            ".streamlit/secrets.toml",
        ):
            self.assertIn(required, gitignore)

        env_example = (project_root / ".env.example").read_text(encoding="utf-8")
        self.assertIn("OPENAI_API_KEY=", env_example)
        self.assertNotRegex(env_example, r"sk-[A-Za-z0-9_-]{20,}")

        requirements = (project_root / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn('audioop-lts; python_version >= "3.13"', requirements)

        repository_root = project_root.parent
        self.assertIn("ffmpeg", (repository_root / "packages.txt").read_text(encoding="utf-8"))
        self.assertTrue((repository_root / ".streamlit" / "config.toml").exists())

        app_source = (project_root / "app.py").read_text(encoding="utf-8")
        self.assertNotIn('ui.section_title("تقدير التكلفة")', app_source)
        self.assertNotIn('ui.section_title("إحصاءات التكلفة")', app_source)
        self.assertIn('"متابعة وبدء العمل"', app_source)
        self.assertIn('getattr(openai_client, "get_monthly_spend_status", None)', app_source)


if __name__ == "__main__":
    unittest.main()
