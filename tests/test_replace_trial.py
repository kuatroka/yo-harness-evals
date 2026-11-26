import json
import tempfile
import unittest
from pathlib import Path

from scripts import replace_trial as rt


def make_trial(root: Path, name: str, task: str, reward, exc=None, valid=True) -> Path:
    trial = root / name
    trial.mkdir(parents=True)
    result = {
        "trial_name": name,
        "task_name": f"terminal-bench/{task}",
        "trial_uri": f"file://{trial}",
        "finished_at": "2026-11-20T00:00:00Z",
        "reward_valid": valid,
        "verifier_result": {"rewards": {"reward": reward}},
        "exception_info": {"exception_type": exc} if exc else None,
        "agent_result": {"n_input_tokens": 10, "n_cache_tokens": 0, "n_output_tokens": 5},
        "config": {"agent": {"model_name": "devin/swe-2-flash-max", "kwargs": {}}},
    }
    (trial / "result.json").write_text(json.dumps(result))
    (trial / "config.json").write_text(json.dumps(result["config"]))
    return trial


def make_job(root: Path, trials: list[tuple[str, str, float, str | None]]) -> Path:
    job = root / "fh-job"
    job.mkdir(parents=True)
    for name, task, reward, exc in trials:
        make_trial(job, name, task, reward, exc)
    (job / "result.json").write_text(
        json.dumps(
            {
                "id": "job-id",
                "n_total_trials": len(trials),
                "stats": {
                    "evals": {
                        "factory-droid__devin/swe-2-flash-max__frontier-harness-eval": {
                            "n_trials": len(trials)
                        }
                    },
                    "n_input_tokens": 0,
                    "n_cache_tokens": 0,
                    "n_output_tokens": 0,
                },
            }
        )
    )
    return job


class ReplaceTrialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / "archive"
        self._saved = rt.ARCHIVE_ROOT
        rt.ARCHIVE_ROOT = self.archive

    def tearDown(self):
        rt.ARCHIVE_ROOT = self._saved
        self.tmp.cleanup()

    def build_job(self) -> Path:
        return make_job(
            self.root,
            [
                ("cancel__a1", "cancel", 1.0, None),
                ("sparql__b2", "sparql", 0.0, "AgentTimeoutError"),
                ("sparql__b3", "sparql", 1.0, None),
            ],
        )

    def test_replacement_updates_stats_and_keeps_name(self):
        job = self.build_job()
        rerun_job = self.root / "fh-rerun"
        rerun_job.mkdir()
        new = make_trial(rerun_job, "sparql__new", "sparql", 1.0)

        rt.replace(job, "sparql__b2", new, dry_run=False)

        stored = json.loads((job / "sparql__b2" / "result.json").read_text())
        self.assertEqual(stored["trial_name"], "sparql__b2")
        self.assertEqual(stored["replaced_from"], "sparql__new")
        self.assertIsNone(stored["exception_info"])

        stats = json.loads((job / "result.json").read_text())["stats"]
        self.assertEqual(stats["n_errored_trials"], 0)
        self.assertEqual(stats["n_completed_trials"], 3)
        self.assertEqual(stats["n_input_tokens"], 30)
        ev = stats["evals"]["factory-droid__devin/swe-2-flash-max__frontier-harness-eval"]
        self.assertAlmostEqual(ev["metrics"][0]["mean"], 1.0)
        self.assertEqual(ev["reward_stats"]["reward"]["1.0"], ["cancel__a1", "sparql__b2", "sparql__b3"])
        self.assertEqual(ev["exception_stats"], {})

    def test_old_trial_is_archived(self):
        job = self.build_job()
        rerun_job = self.root / "fh-rerun"
        rerun_job.mkdir()
        new = make_trial(rerun_job, "sparql__new", "sparql", 1.0)

        rt.replace(job, "sparql__b2", new, dry_run=False)

        archived = list(self.archive.glob("fh-job/sparql__b2-*"))
        self.assertEqual(len(archived), 1)
        old = json.loads((archived[0] / "result.json").read_text())
        self.assertEqual(old["exception_info"]["exception_type"], "AgentTimeoutError")

    def test_dry_run_touches_nothing(self):
        job = self.build_job()
        rerun_job = self.root / "fh-rerun"
        rerun_job.mkdir()
        new = make_trial(rerun_job, "sparql__new", "sparql", 1.0)
        before = (job / "result.json").read_text()

        summary = rt.replace(job, "sparql__b2", new, dry_run=True)

        self.assertEqual(summary["new_reward"], 1.0)
        self.assertEqual((job / "result.json").read_text(), before)
        self.assertFalse(self.archive.exists())

    def test_unfinished_replacement_is_refused(self):
        job = self.build_job()
        rerun_job = self.root / "fh-rerun"
        rerun_job.mkdir()
        new = make_trial(rerun_job, "sparql__new", "sparql", 1.0)
        data = json.loads((new / "result.json").read_text())
        data["finished_at"] = None
        (new / "result.json").write_text(json.dumps(data))

        with self.assertRaises(SystemExit):
            rt.replace(job, "sparql__b2", new, dry_run=False)

    def test_task_mismatch_is_refused(self):
        job = self.build_job()
        rerun_job = self.root / "fh-rerun"
        rerun_job.mkdir()
        new = make_trial(rerun_job, "other__new", "other", 1.0)

        with self.assertRaises(SystemExit):
            rt.replace(job, "sparql__b2", new, dry_run=False)


if __name__ == "__main__":
    unittest.main()
