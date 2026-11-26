from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harnesses.common import amp_usage_totals, droid_usage_totals, normalize_usage


def _write_log(lines: list[str]) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    )
    with handle:
        handle.write("\n".join(lines) + "\n")
    return Path(handle.name)


class NormalizeUsageTests(unittest.TestCase):
    def test_anthropic_usage_adds_cache_to_input(self) -> None:
        self.assertEqual(
            normalize_usage(
                {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 7,
                }
            ),
            (125, 7, 25),
        )

    def test_openai_prompt_tokens_already_include_cache(self) -> None:
        self.assertEqual(
            normalize_usage({"prompt_tokens": 100, "completion_tokens": 7}),
            (100, 7, 0),
        )

    def test_null_fields_are_zero(self) -> None:
        self.assertEqual(
            normalize_usage(
                {
                    "input_tokens": None,
                    "cache_read_input_tokens": None,
                    "cache_creation_input_tokens": None,
                    "output_tokens": None,
                }
            ),
            (0, 0, 0),
        )


class DroidUsageTests(unittest.TestCase):
    def test_reads_result_usage_with_stderr_noise(self) -> None:
        path = _write_log(
            [
                "warning: something on stderr",
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "usage": {
                            "input_tokens": 12530,
                            "output_tokens": 3,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    }
                ),
            ]
        )
        self.assertEqual(droid_usage_totals(path), (12530, 3, 0))
        path.unlink()

    def test_only_last_result_event_counts(self) -> None:
        path = _write_log(
            [
                json.dumps(
                    {
                        "type": "result",
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "usage": {"input_tokens": 40, "output_tokens": 4},
                    }
                ),
            ]
        )
        self.assertEqual(droid_usage_totals(path), (40, 4, 0))
        path.unlink()

    def test_missing_log_returns_none(self) -> None:
        self.assertIsNone(droid_usage_totals(Path("/nonexistent/droid.txt")))

    def test_no_result_event_returns_none(self) -> None:
        path = _write_log(['{"type":"system","subtype":"init"}'])
        self.assertIsNone(droid_usage_totals(path))
        path.unlink()


class AmpUsageTests(unittest.TestCase):
    def test_sums_per_turn_assistant_usage(self) -> None:
        path = _write_log(
            [
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "session_id": "T-1",
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "type": "message",
                            "role": "assistant",
                            "usage": {
                                "input_tokens": 0,
                                "cache_creation_input_tokens": 41068,
                                "cache_read_input_tokens": 0,
                                "output_tokens": 60,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "type": "message",
                            "role": "assistant",
                            "usage": {
                                "input_tokens": 0,
                                "cache_creation_input_tokens": 2237,
                                "cache_read_input_tokens": 0,
                                "output_tokens": 4,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                    }
                ),
            ]
        )
        self.assertEqual(amp_usage_totals(path), (43305, 64, 43305))
        path.unlink()

    def test_error_result_without_usage_returns_none(self) -> None:
        path = _write_log(
            [
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "error": "Unknown model provider",
                    }
                )
            ]
        )
        self.assertIsNone(amp_usage_totals(path))
        path.unlink()

    def test_duplicate_message_id_is_counted_once(self) -> None:
        usage = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 2,
        }
        message = {"id": "msg-1", "type": "message", "usage": usage}
        path = _write_log(
            [
                json.dumps({"type": "assistant", "message": message}),
                json.dumps({"type": "assistant", "message": message}),
            ]
        )
        self.assertEqual(amp_usage_totals(path), (10, 2, 0))
        path.unlink()


if __name__ == "__main__":
    unittest.main()
