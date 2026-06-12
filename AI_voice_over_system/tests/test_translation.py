from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from src import translation
from src.config import load_settings
from src.logging_utils import close_logging
from src.storage import read_json, write_json


def response(segments: list[dict], prompt_tokens: int = 10, completion_tokens: int = 5) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps({"segments": segments})}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def raw_response(content: str, prompt_tokens: int, completion_tokens: int) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class TranslationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = replace(
            load_settings(),
            jobs_dir=root / "jobs",
            sqlite_path=root / "jobs" / "jobs.db",
            app_log_path=root / "logs" / "app.log",
            translation_batch_size=20,
            translation_recovery_batch_size=5,
        )
        self.job_path = root / "jobs" / "test-job"
        self.job_path.mkdir(parents=True)
        self.segments = [
            {"id": 1, "start": 0.0, "end": 1.0, "source_text": "One"},
            {"id": 2, "start": 1.0, "end": 2.0, "source_text": "Two"},
            {"id": 3, "start": 2.0, "end": 3.0, "source_text": "Three"},
        ]

    def tearDown(self) -> None:
        close_logging(self.settings)
        self.temp_dir.cleanup()

    def test_recovers_missing_id_without_discarding_valid_translations(self) -> None:
        model_responses = [
            response(
                [
                    {"id": 1, "target_text": "Uno"},
                    {"id": 3, "target_text": "Tres"},
                    {"id": 999, "target_text": "Unexpected"},
                ]
            ),
            response([{"id": 200, "target_text": "Dos"}], prompt_tokens=4, completion_tokens=2),
        ]

        with (
            patch("src.translation.get_client", return_value=object()),
            patch("src.translation._chat_json", side_effect=model_responses) as chat,
        ):
            translated, usage = translation.translate_segments(
                self.settings,
                self.job_path,
                self.segments,
                "es",
                "Spanish",
            )

        self.assertEqual([item["target_text"] for item in translated], ["Uno", "Dos", "Tres"])
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(usage, {"prompt_tokens": 14, "completion_tokens": 7, "total_tokens": 21})
        self.assertTrue((self.job_path / "translation_es.json").exists())
        self.assertTrue((self.job_path / "translation_es.srt").exists())
        self.assertFalse((self.job_path / "translation_es_progress.json").exists())

    def test_resumes_from_translation_checkpoint(self) -> None:
        write_json(
            self.job_path / "translation_es_progress.json",
            {
                "model": self.settings.openai_text_model,
                "language": "es",
                "source_ids": [1, 2, 3],
                "translations": {"1": "Uno", "2": "Dos"},
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            },
        )

        with (
            patch("src.translation.get_client", return_value=object()),
            patch(
                "src.translation._chat_json",
                return_value=response([{"id": 3, "target_text": "Tres"}], 3, 2),
            ) as chat,
        ):
            translated, usage = translation.translate_segments(
                self.settings,
                self.job_path,
                self.segments,
                "es",
                "Spanish",
            )

        self.assertEqual(chat.call_count, 1)
        self.assertEqual([item["target_text"] for item in translated], ["Uno", "Dos", "Tres"])
        self.assertEqual(usage, {"prompt_tokens": 23, "completion_tokens": 12, "total_tokens": 35})
        saved = read_json(self.job_path / "translation_es.json")
        self.assertEqual(len(saved["segments"]), 3)

    def test_malformed_json_recovers_and_keeps_usage_cost(self) -> None:
        model_responses = [
            raw_response("not valid json", 7, 3),
            response(
                [
                    {"id": 1, "target_text": "Uno"},
                    {"id": 2, "target_text": "Dos"},
                    {"id": 3, "target_text": "Tres"},
                ]
            ),
        ]

        with (
            patch("src.translation.get_client", return_value=object()),
            patch("src.translation._chat_json", side_effect=model_responses) as chat,
        ):
            translated, usage = translation.translate_segments(
                self.settings,
                self.job_path,
                self.segments,
                "es",
                "Spanish",
            )

        self.assertEqual(chat.call_count, 2)
        self.assertEqual([item["target_text"] for item in translated], ["Uno", "Dos", "Tres"])
        self.assertEqual(usage, {"prompt_tokens": 17, "completion_tokens": 8, "total_tokens": 25})

    def test_legacy_strict_id_error_is_not_in_translation_module(self) -> None:
        source = Path(translation.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Translation segment IDs do not match the source.", source)
        self.assertNotIn("def _repair_translation", source)


if __name__ == "__main__":
    unittest.main()
