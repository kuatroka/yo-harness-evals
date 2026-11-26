import json
import tempfile
import unittest
from pathlib import Path

from harbor.models.job.config import JobConfig
from harbor.models.trial.config import AgentConfig, TrialConfig

from scripts import upload_phoenix as up

TASKS = ("alacritty__alacritty-3921", "burntsushi__ripgrep-2555")


def agent(**kwargs) -> AgentConfig:
    return AgentConfig(
        name="harnesses.droid:DroidAgent",
        model_name="devin/haiku-5-5-max",
        kwargs=kwargs,
        skip_model_parsing=True,
    )


def trial_config(task: str, trial_name: str, agent_config: AgentConfig) -> dict:
    return TrialConfig(
        task={"path": f"/tmp/bench-fast/{task}", "source": "bench-fast"},
        agent=agent_config,
        trial_name=trial_name,
    ).model_dump(mode="json")


def add_trial(job: Path, task: str, suffix: str, agent_config: AgentConfig, write_config=True) -> Path:
    trial = job / f"{task}__{suffix}"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"trial_name": trial.name}))
    if write_config:
        (trial / "config.json").write_text(
            json.dumps(trial_config(task, trial.name, agent_config))
        )
    return trial


def make_job(root: Path, trials: list[tuple[str, str, bool]]) -> Path:
    job = root / "fh-job"
    job.mkdir()
    for task, suffix, write_config in trials:
        add_trial(job, task, suffix, agent(), write_config)
    (job / "result.json").write_text(json.dumps({"id": "job-id"}))
    return job


class ReconcileAgentTest(unittest.TestCase):
    def test_adopts_the_agent_every_trial_recorded(self):
        config = JobConfig.model_validate(
            {"job_name": "j", "jobs_dir": "/tmp", "agents": [agent()]}
        )
        configs = [trial_config(TASKS[0], "a__1", agent(reasoning_effort="max"))]

        message = up.reconcile_single_agent(config, configs)

        self.assertIn("reasoning_effort", message)
        self.assertEqual(config.agents[0].kwargs, {"reasoning_effort": "max"})

    def test_leaves_a_matching_job_alone(self):
        config = JobConfig.model_validate(
            {"job_name": "j", "jobs_dir": "/tmp", "agents": [agent(effort="max")]}
        )
        configs = [trial_config(TASKS[0], "a__1", agent(effort="max"))]

        self.assertIsNone(up.reconcile_single_agent(config, configs))
        self.assertEqual(config.agents[0].kwargs, {"effort": "max"})

    def test_leaves_a_job_whose_trials_disagree_alone(self):
        config = JobConfig.model_validate(
            {"job_name": "j", "jobs_dir": "/tmp", "agents": [agent()]}
        )
        configs = [
            trial_config(TASKS[0], "a__1", agent(effort="max")),
            trial_config(TASKS[0], "a__2", agent(effort="high")),
        ]

        self.assertIsNone(up.reconcile_single_agent(config, configs))
        self.assertEqual(config.agents[0].kwargs, {})


class SynthesizeConfigTest(unittest.TestCase):
    def test_counts_finished_trial_dirs_not_readable_configs(self):
        # The trial whose config went missing still holds a result, so the
        # rebuilt job has to plan a slot for it or Harbor rejects the job.
        with tempfile.TemporaryDirectory() as tmp:
            job = make_job(
                Path(tmp),
                [
                    (TASKS[0], "a1", True),
                    (TASKS[0], "a2", True),
                    (TASKS[0], "a3", True),
                    (TASKS[0], "a4", False),
                    (TASKS[1], "r1", True),
                    (TASKS[1], "r2", True),
                    (TASKS[1], "r3", True),
                ],
            )

            config = up.synthesize_config(job, up.trial_configs(job))

            self.assertEqual(config.n_attempts, 4)
            self.assertEqual(config.datasets[0].task_names, sorted(TASKS))

    def test_refuses_when_there_is_nothing_to_rebuild_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "empty"
            job.mkdir()

            with self.assertRaises(SystemExit):
                up.synthesize_config(job, [])


class StageMirrorTest(unittest.TestCase):
    def test_fills_a_missing_trial_config_without_touching_the_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = make_job(
                Path(tmp),
                [(TASKS[0], "a1", True), (TASKS[0], "a2", False)],
            )
            config = up.synthesize_config(job, up.trial_configs(job))

            root = up.stage_mirror(job, config)
            try:
                mirror = root / job.name
                self.assertTrue((mirror / "config.json").is_file())
                self.assertTrue((mirror / "result.json").is_file())
                filled = json.loads((mirror / f"{TASKS[0]}__a2" / "config.json").read_text())
                self.assertEqual(filled["trial_name"], f"{TASKS[0]}__a2")
                self.assertEqual(filled["trials_dir"], str(mirror))
                # The real trial never had a config and the mirror did not add one.
                self.assertFalse((job / f"{TASKS[0]}__a2" / "config.json").exists())
            finally:
                up.shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
