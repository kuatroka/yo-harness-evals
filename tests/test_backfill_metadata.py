import json
import tempfile
import unittest
from pathlib import Path

from scripts.backfill_metadata import (
    backfill_trial,
    effort_from_model,
    iter_trials,
    patch_config,
)


class EffortFromModelTest(unittest.TestCase):
    def test_reads_suffix_of_model_name(self):
        cases = {
            "devin/swe-2-max": "max",
            "devin/swe-2-flash-max": "max",
            "devin/deepseek-v4.1-flash-high": "high",
            "engy/deepseek-v4.1-flash-1017(max)": "max",
            "ag/gemini-4-carbon-xhigh": None,
        }
        for model_name, expected in cases.items():
            with self.subTest(model_name=model_name):
                self.assertEqual(effort_from_model(model_name), expected)

    def test_returns_none_without_suffix(self):
        for model_name in ("devin/swe-2", "gpt-5", None, ""):
            with self.subTest(model_name=model_name):
                self.assertIsNone(effort_from_model(model_name))


class PatchConfigTest(unittest.TestCase):
    def test_fills_missing_effort(self):
        config = {"agent": {"model_name": "devin/swe-2-flash-max", "kwargs": {}}}
        self.assertEqual(patch_config(config), "max")
        self.assertEqual(config["agent"]["kwargs"]["reasoning_effort"], "max")

    def test_leaves_existing_effort_alone(self):
        config = {
            "agent": {
                "model_name": "devin/swe-2-flash-max",
                "kwargs": {"reasoning_effort": "high"},
            }
        }
        self.assertIsNone(patch_config(config))
        self.assertEqual(config["agent"]["kwargs"]["reasoning_effort"], "high")

    def test_missing_kwargs_is_created(self):
        config = {"agent": {"model_name": "devin/swe-2-max"}}
        self.assertEqual(patch_config(config), "max")
        self.assertEqual(config["agent"]["kwargs"], {"reasoning_effort": "max"})

    def test_no_model_yields_no_change(self):
        self.assertIsNone(patch_config({"agent": {"kwargs": {}}}))
        self.assertIsNone(patch_config({}))


class BackfillTrialTest(unittest.TestCase):
    def write_trial(self, root: Path, config: dict) -> Path:
        trial = root / "task__abc"
        trial.mkdir()
        result = {"trial_name": "task__abc", "reward_valid": True, "config": config}
        (trial / "result.json").write_text(json.dumps(result, indent=4))
        return trial

    def test_writes_result_and_config(self):
        config = {"agent": {"model_name": "devin/swe-2-max", "kwargs": {}}}
        with tempfile.TemporaryDirectory() as tmp:
            trial = self.write_trial(Path(tmp), config)
            self.assertEqual(backfill_trial(trial, dry_run=False), "max")
            stored = json.loads((trial / "result.json").read_text())
            self.assertEqual(stored["config"]["agent"]["kwargs"]["reasoning_effort"], "max")
            self.assertEqual(stored["reward_valid"], True)
            on_disk = json.loads((trial / "config.json").read_text())
            self.assertEqual(on_disk["agent"]["kwargs"]["reasoning_effort"], "max")

    def test_dry_run_touches_nothing(self):
        config = {"agent": {"model_name": "devin/swe-2-max", "kwargs": {}}}
        with tempfile.TemporaryDirectory() as tmp:
            trial = self.write_trial(Path(tmp), config)
            before = (trial / "result.json").read_text()
            self.assertEqual(backfill_trial(trial, dry_run=True), "max")
            self.assertEqual((trial / "result.json").read_text(), before)
            self.assertFalse((trial / "config.json").exists())


class IterTrialsTest(unittest.TestCase):
    def build(self, root: Path) -> None:
        job = root / "fh-job"
        job.mkdir()
        (job / "result.json").write_text("{}")
        for name in ("task-a__1", "task-b__2"):
            trial = job / name
            trial.mkdir()
            (trial / "result.json").write_text("{}")

    def test_finds_trials_when_root_is_jobs_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.build(Path(tmp))
            names = [p.name for p in iter_trials(Path(tmp))]
            self.assertEqual(names, ["task-a__1", "task-b__2"])

    def test_finds_trials_when_root_is_one_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.build(Path(tmp))
            names = [p.name for p in iter_trials(Path(tmp) / "fh-job")]
            self.assertEqual(names, ["task-a__1", "task-b__2"])

    def test_job_result_is_not_a_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.build(Path(tmp))
            self.assertNotIn("fh-job", [p.name for p in iter_trials(Path(tmp))])


if __name__ == "__main__":
    unittest.main()
