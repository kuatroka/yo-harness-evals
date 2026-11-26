from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.backfill_tokens import (
    backfill_trial,
    codex_usage,
    is_eligible,
    iter_trials,
)

DROID_LOG = (
    '{"type":"result","usage":'
    '{"input_tokens":100,"output_tokens":20,"cache_read_input_tokens":30}}\n'
)


def result_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "finished_at": "2026-11-18T00:00:00Z",
        "exception_info": None,
        "verifier_result": {"rewards": {"reward": 1.0}},
        "agent_result": {
            "n_input_tokens": None,
            "n_output_tokens": None,
            "n_cache_tokens": None,
            "model_usage": None,
            "metadata": {"harness": "factory-droid"},
        },
        "config": {"agent": {"model_name": "devin/swe-2-max"}},
    }
    document.update(overrides)
    return document


class BackfillTokensTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_trial(
        self, result: dict[str, object], agent_file: str | None = None, content: str = ""
    ) -> Path:
        job = self.root / "job-1"
        (job / "result.json").parent.mkdir(parents=True, exist_ok=True)
        (job / "result.json").write_text("{}")
        trial = job / "task__abc"
        (trial / "agent").mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps(result, indent=4))
        if agent_file:
            (trial / "agent" / agent_file).write_text(content)
        return trial

    def load(self, trial: Path) -> dict:
        return json.loads((trial / "result.json").read_text())

    def test_fills_tokens_from_the_droid_result_event(self) -> None:
        trial = self.write_trial(result_document(), "droid.txt", DROID_LOG)

        usage = backfill_trial(trial, dry_run=False)

        self.assertEqual(usage, (130, 20, 30))
        agent_result = self.load(trial)["agent_result"]
        self.assertEqual(agent_result["n_input_tokens"], 130)
        self.assertEqual(agent_result["n_output_tokens"], 20)
        self.assertEqual(agent_result["n_cache_tokens"], 30)

    def test_leaves_an_errored_trial_alone(self) -> None:
        result = result_document(exception_info={"type": "ApiOverloadedError"})
        trial = self.write_trial(result, "droid.txt", DROID_LOG)

        self.assertIsNone(backfill_trial(trial, dry_run=False))
        self.assertIsNone(self.load(trial)["agent_result"]["n_input_tokens"])

    def test_leaves_a_trial_that_already_has_tokens_alone(self) -> None:
        result = result_document()
        result["agent_result"]["n_input_tokens"] = 5
        result["agent_result"]["n_output_tokens"] = 2
        trial = self.write_trial(result, "droid.txt", DROID_LOG)

        self.assertIsNone(backfill_trial(trial, dry_run=False))
        self.assertEqual(self.load(trial)["agent_result"]["n_input_tokens"], 5)

    def test_leaves_a_trial_without_a_recorded_reward_alone(self) -> None:
        result = result_document(verifier_result=None)
        trial = self.write_trial(result, "droid.txt", DROID_LOG)

        self.assertIsNone(backfill_trial(trial, dry_run=False))
        self.assertIsNone(self.load(trial)["agent_result"]["n_input_tokens"])

    def test_logs_without_usage_stay_null(self) -> None:
        trial = self.write_trial(result_document(), "amp.txt", "only the final text\n")

        self.assertIsNone(backfill_trial(trial, dry_run=False))
        self.assertIsNone(self.load(trial)["agent_result"]["n_input_tokens"])

    def test_codex_zero_usage_counts_as_data(self) -> None:
        stream = (
            'not json\n'
            '{"type":"turn.completed","usage":{"input_tokens":0,'
            '"output_tokens":0,"cached_input_tokens":0}}\n'
        )
        trial = self.write_trial(result_document(), "codex.txt", stream)

        usage = backfill_trial(trial, dry_run=False)

        self.assertEqual(usage, (0, 0, 0))
        self.assertEqual(self.load(trial)["agent_result"]["n_input_tokens"], 0)

    def test_dry_run_writes_nothing(self) -> None:
        trial = self.write_trial(result_document(), "droid.txt", DROID_LOG)

        self.assertEqual(backfill_trial(trial, dry_run=True), (130, 20, 30))
        self.assertIsNone(self.load(trial)["agent_result"]["n_input_tokens"])

    def test_output_keeps_harbors_formatting(self) -> None:
        trial = self.write_trial(result_document(), "droid.txt", DROID_LOG)

        backfill_trial(trial, dry_run=False)

        raw = (trial / "result.json").read_text()
        self.assertEqual(raw, json.dumps(json.loads(raw), indent=4))


class EligibilityTest(unittest.TestCase):
    def test_requires_a_finished_clean_scored_trial(self) -> None:
        self.assertTrue(is_eligible(result_document()))
        self.assertFalse(is_eligible(result_document(finished_at=None)))
        self.assertFalse(is_eligible(result_document(exception_info={})))
        self.assertFalse(is_eligible(result_document(verifier_result=None)))


class CodexUsageTest(unittest.TestCase):
    def test_last_turn_completed_wins(self) -> None:
        with TemporaryDirectory() as tmp:
            stream = Path(tmp) / "codex.txt"
            stream.write_text(
                '{"type":"turn.completed","usage":{"input_tokens":10,'
                '"output_tokens":1,"cached_input_tokens":4}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":30,'
                '"output_tokens":3,"cached_input_tokens":12}}\n'
            )

            self.assertEqual(codex_usage(stream), (30, 3, 12))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(codex_usage(Path("/nonexistent/codex.txt")))


class IterTrialsTest(unittest.TestCase):
    def test_walks_a_jobs_root_through_its_jobs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            trial = root / "job-1" / "task__abc"
            trial.mkdir(parents=True)
            (trial / "result.json").write_text("{}")
            (root / "job-1" / "result.json").write_text("{}")

            self.assertEqual(iter_trials(root), [trial])

    def test_accepts_a_single_job_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            job = Path(tmp) / "job-1"
            trial = job / "task__abc"
            trial.mkdir(parents=True)
            (trial / "result.json").write_text("{}")
            (job / "result.json").write_text("{}")

            self.assertEqual(iter_trials(job), [trial])
