import json
import tempfile
import unittest
from pathlib import Path

from viewer_overlay.display import display_agent_name, display_model_name
from viewer_overlay.middleware import rewrite_json


class DisplayAgentNameTest(unittest.TestCase):
    def test_known_agents_map_to_short_names(self):
        cases = {
            "harnesses.amp:AmpAgent": "Amp",
            "amp-code": "Amp",
            "harnesses.droid:DroidAgent": "Droid",
            "factory-droid": "Droid",
            "harnesses.claude:ClaudeCodeAgent": "Claude Code",
            "claude-code": "Claude Code",
            "harnesses.codex:Codex": "Codex",
            "harbor.agents.installed.codex:Codex": "Codex",
            "codex": "Codex",
            "Codex": "Codex",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(display_agent_name(raw), expected)

    def test_unknown_class_name_falls_back_to_class(self):
        self.assertEqual(display_agent_name("harnesses.other:OtherAgent"), "OtherAgent")

    def test_missing_name_passes_through(self):
        self.assertIsNone(display_agent_name(None))
        self.assertEqual(display_agent_name(""), "")


class DisplayModelNameTest(unittest.TestCase):
    def test_route_prefix_is_dropped(self):
        self.assertEqual(
            display_model_name("engy/deepseek-v4.1-flash-1017(max)"),
            "deepseek-v4.1-flash-1017(max)",
        )
        self.assertEqual(display_model_name("devin/haiku-5-5-max"), "haiku-5-5(max)")

    def test_effort_suffix_becomes_parentheses(self):
        cases = {
            "devin/swe-2-max": "swe-2(max)",
            "devin/swe-2-flash-max": "swe-2-flash(max)",
            "devin/swe-2-1-max": "swe-2-1(max)",
            "devin/swe-2-1-xhigh": "swe-2-1(xhigh)",
            "devin/deepseek-v4.1-flash-high": "deepseek-v4.1-flash(high)",
            "ag/gemini-4-carbon-xhigh": "gemini-4-carbon(xhigh)",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(display_model_name(raw), expected)

    def test_names_without_effort_are_only_unprefixed(self):
        self.assertEqual(display_model_name("cc/claude-fable-5-2"), "claude-fable-5-2")
        self.assertEqual(display_model_name("swe-2"), "swe-2")

    def test_inner_slash_survives(self):
        self.assertEqual(
            display_model_name("litho/moonshotai/Kimi-K3.1-Pro-ultra"),
            "moonshotai/Kimi-K3.1-Pro-ultra",
        )


class EffortFromTrialTest(unittest.TestCase):
    """A direct Anthropic id carries no effort, so the trial field names it."""

    def test_effort_field_becomes_parentheses(self):
        self.assertEqual(display_model_name("claude-haiku-5-5", "max"), "claude-haiku-5-5(max)")
        self.assertEqual(
            display_model_name("anthropic/claude-haiku-5-5", "MAX"), "claude-haiku-5-5(max)"
        )

    def test_never_repeats_a_level_the_id_already_names(self):
        self.assertEqual(
            display_model_name("engy/deepseek-v4.1-flash-1017(max)", "max"),
            "deepseek-v4.1-flash-1017(max)",
        )
        self.assertEqual(display_model_name("devin/swe-2-max", "max"), "swe-2(max)")

    def test_unknown_effort_passes_through(self):
        self.assertEqual(display_model_name("claude-haiku-5-5", "turbo"), "claude-haiku-5-5")
        self.assertEqual(display_model_name("claude-haiku-5-5"), "claude-haiku-5-5")


class ResponseBoundaryTest(unittest.TestCase):
    """The API response boundary is where the UI now gets its labels.

    These assertions used to run against validators patched into
    ``harbor.viewer.models``. Harbor is pristine now, so the same expectations
    are held against the overlay's rewrite instead.
    """

    def test_job_summary_maps_and_dedupes(self):
        job = rewrite_json(
            {
                "name": "job",
                "agents": ["harnesses.amp:AmpAgent", "amp-code", "harnesses.droid:DroidAgent"],
                "models": ["devin/swe-2-max", "devin/swe-2-flash-max"],
            }
        )
        self.assertEqual(job["agents"], ["Amp", "Droid"])
        self.assertEqual(job["models"], ["swe-2(max)", "swe-2-flash(max)"])

    def test_trial_summary_maps_agent_and_model(self):
        trial = rewrite_json(
            {
                "name": "trial",
                "task_name": "task",
                "agent_name": "harnesses.claude:ClaudeCodeAgent",
                "reported_agent_name": "claude-code",
                "model_name": "engy/deepseek-v4.1-flash-1017(max)",
            }
        )
        self.assertEqual(trial["agent_name"], "Claude Code")
        self.assertEqual(trial["reported_agent_name"], "Claude Code")
        self.assertEqual(trial["model_name"], "deepseek-v4.1-flash-1017(max)")

    def test_task_summary_maps_agent_and_model(self):
        task = rewrite_json(
            {
                "task_name": "task",
                "agent_name": "factory-droid",
                "model_name": "devin/haiku-5-5-max",
            }
        )
        self.assertEqual(task["agent_name"], "Droid")
        self.assertEqual(task["model_name"], "haiku-5-5(max)")

    def test_comparison_row_maps_agent_and_model(self):
        row = rewrite_json(
            {
                "job_name": "job",
                "agent_name": "harnesses.amp:AmpAgent",
                "model_name": "devin/haiku-5-5-max",
                "key": "job::amp::devin::haiku-5-5-max",
            }
        )
        self.assertEqual(row["agent_name"], "Amp")
        self.assertEqual(row["model_name"], "haiku-5-5(max)")
        self.assertEqual(row["key"], "job::amp::devin::haiku-5-5-max")

    def test_trial_uses_its_reasoning_effort(self):
        trial = rewrite_json(
            {
                "name": "trial",
                "task_name": "task",
                "model_name": "claude-haiku-5-5",
                "reasoning_effort": "max",
            }
        )
        self.assertEqual(trial["model_name"], "claude-haiku-5-5(max)")

    def test_job_model_uses_effort_from_its_trial_config(self):
        cases = (
            (
                "amp-job",
                "harnesses.amp:AmpAgent",
                "anthropic/claude-opus-5-5",
                {"effort": "medium"},
                "Amp",
            ),
            (
                "claude-job",
                "harnesses.claude:ClaudeCodeAgent",
                "claude-opus-5-5",
                {"reasoning_effort": "medium"},
                "Claude Code",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp)
            for job_name, agent_name, model_name, kwargs, display_agent in cases:
                trial = jobs / job_name / "task__x"
                trial.mkdir(parents=True)
                (trial / "config.json").write_text(
                    json.dumps(
                        {
                            "agent": {
                                "name": agent_name,
                                "model_name": model_name,
                                "kwargs": kwargs,
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                job = rewrite_json(
                    {
                        "name": job_name,
                        "datasets": ["frontier-harness-eval"],
                        "agents": [display_agent],
                        "providers": ["anthropic"],
                        "models": ["claude-opus-5-5"],
                        "environment_type": "docker",
                    },
                    jobs_dir=jobs,
                )
                with self.subTest(job=job_name):
                    self.assertEqual(job["reasoning_effort"], "medium")
                    self.assertEqual(job["models"], ["claude-opus-5-5(medium)"])


if __name__ == "__main__":
    unittest.main()
