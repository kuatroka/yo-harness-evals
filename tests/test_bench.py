import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

import calibrate  # noqa: E402
import model_id  # noqa: E402
import lib  # noqa: E402
import plan  # noqa: E402
import screen  # noqa: E402
import report  # noqa: E402

RULE = {"min_discordant": 6, "min_gap": 0.10, "min_tasks": 3}


def trial(task, reward, *, timed_out=False, minutes=3.0, harness="droid", model="devin/swe-2-max"):
    return lib.Trial(
        job="job",
        harness=harness,
        model=model,
        task=task,
        reward=reward,
        timed_out=timed_out,
        duration_min=minutes,
        finished=True,
    )


class PercentileTest(unittest.TestCase):
    def test_nearest_rank(self):
        self.assertIsNone(lib.percentile([], 0.5))
        self.assertEqual(lib.percentile([7.0], 0.9), 7.0)
        self.assertEqual(lib.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5), 3.0)
        self.assertEqual(lib.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.9), 5.0)


class ResolveModelTest(unittest.TestCase):
    def setUp(self):
        self.models = lib.load_config("models")

    def test_maps_every_route(self):
        # A paused row (empty harnesses) defers to an enabled row on the same
        # route. Effort variants that share one physical model id also share a
        # route (claude-haiku-5-5 carries no effort), so several enabled rows
        # may resolve to the first enabled match.
        first_enabled: dict[str, str] = {}
        for key, spec in self.models.items():
            if spec.get("harnesses") and spec["route"] not in first_enabled:
                first_enabled[spec["route"]] = key
        for key, spec in self.models.items():
            with self.subTest(key=key):
                resolved = lib.resolve_model(spec["route"], self.models)
                self.assertIsNotNone(resolved)
                if spec.get("harnesses"):
                    self.assertEqual(resolved, first_enabled[spec["route"]])

    def test_paused_row_resolves_to_the_enabled_row(self):
        # haiku-anthropic-direct is retired (empty harnesses); the haiku-5.5-*
        # rows now own the native Anthropic route for Claude Code.
        self.assertEqual(
            lib.resolve_model("claude-haiku-5-5", self.models), "haiku-5.5-max"
        )

    def test_strips_custom_prefix_and_dv_alias(self):
        self.assertEqual(lib.resolve_model("custom:devin/swe-2-max", self.models), "swe-2-max")
        self.assertEqual(lib.resolve_model("dv/swe-2-max", self.models), "swe-2-max")

    def test_model_cap_narrows_concurrency(self):
        registry = {"swe-2-max": {"max_concurrency": 2, "harnesses": ["droid"]}}
        caps = {"droid": {"max_concurrency": 8}}
        self.assertEqual(
            plan.concurrency_cap(["droid"], ["swe-2-max"], registry, caps), 2
        )

    def test_harness_cap_narrows_concurrency(self):
        registry = {"swe-2-max": {"harnesses": ["droid"]}}
        caps = {"droid": {"max_concurrency": 2}, "amp": {"max_concurrency": 6}}
        self.assertEqual(
            plan.concurrency_cap(["droid", "amp"], ["swe-2-max"], registry, caps), 2
        )

    def test_unknown_route_stays_unknown(self):
        self.assertIsNone(lib.resolve_model("nobody/unknown-model", self.models))


class OverBudgetTest(unittest.TestCase):
    def _result(self, task_path, start, end):
        return {
            "config": {"task": {"path": str(task_path)}, "timeout_multiplier": 1.0},
            "agent_execution": {"started_at": start, "finished_at": end},
        }

    def _task(self, seconds=600.0):
        import tempfile

        tmp = tempfile.mkdtemp()
        Path(tmp, "task.toml").write_text(f"[agent]\ntimeout_sec = {seconds}\n")
        return tmp

    def test_agent_phase_past_its_budget_is_censored_without_an_exception(self):
        # A Claude trial ran 25 minutes against a 600 second budget and finished
        # with no exception. Wall clock censors it even when the flag is missing.
        result = self._result(
            self._task(), "2026-11-21T10:57:37Z", "2026-11-21T11:23:41Z"
        )
        self.assertTrue(lib.ran_over_budget(result))
        self.assertAlmostEqual(lib.agent_budget_minutes(result), 10.0)

    def test_agent_phase_inside_its_budget_is_not_censored(self):
        result = self._result(
            self._task(), "2026-11-21T10:57:37Z", "2026-11-21T11:06:37Z"
        )
        self.assertFalse(lib.ran_over_budget(result))

    def test_a_trial_with_no_task_path_is_not_censored(self):
        result = self._result("", "2026-11-21T10:57:37Z", "2026-11-21T11:23:41Z")
        result["config"] = {"timeout_multiplier": 1.0}
        self.assertIsNone(lib.agent_budget_minutes(result))
        self.assertFalse(lib.ran_over_budget(result))


class SummariseTest(unittest.TestCase):
    def test_timeout_counts_as_zero_but_stays_out_of_the_raw_mean(self):
        cell = lib.summarise([trial("t", 1.0), trial("t", 0.0), trial("t", None, timed_out=True)])
        self.assertEqual(cell.n_usable, 2)
        self.assertEqual(cell.n_timeout, 1)
        self.assertEqual(cell.mean_without_timeouts, 0.5)
        self.assertAlmostEqual(cell.mean_with_timeouts, 1 / 3)

    def test_censored_durations_stay_out_of_the_percentiles(self):
        # A hang can end at 40 minutes on a ten-minute budget. The percentile
        # measures the task, so only the uncensored trial may set it.
        cell = lib.summarise(
            [trial("t", 1.0, minutes=3.0), trial("t", 0.0, timed_out=True, minutes=40.0)]
        )
        self.assertEqual(cell.p50_minutes, 3.0)
        self.assertEqual(cell.p90_minutes, 3.0)
        self.assertEqual(cell.n_timeout, 1)

    def test_a_trial_that_times_out_after_landing_is_scored_once(self):
        # The verifier can pass on work that landed before the budget stopped the
        # process. That trial is a timeout, not half a pass.
        cell = lib.summarise([trial("t", 1.0, timed_out=True), trial("t", 1.0)])
        self.assertEqual(cell.n_timeout, 1)
        self.assertEqual(cell.n_usable, 1)
        self.assertEqual(cell.mean_without_timeouts, 1.0)
        self.assertEqual(cell.mean_with_timeouts, 0.5)


class CalibrationTest(unittest.TestCase):
    def test_censored_duration_does_not_move_the_percentile(self):
        models = lib.load_config("models")
        trials = [
            trial("task", 1.0, minutes=3.0),
            trial("task", 1.0, minutes=4.0),
            trial("task", None, timed_out=True, minutes=15.0),
        ]
        measured = calibrate.measure(trials, models)["task"]
        self.assertEqual(measured["p90"]["swe-2"], 4.0)
        self.assertNotIn(15.0, measured["p50"].values())

    def test_tier_follows_the_gate(self):
        tasks = {"fast": {"p90": {"swe-2": 5.9}}, "slow": {"p90": {"swe-2": 6.1}}}
        calibrate.classify(tasks, 6.0)
        self.assertEqual(tasks["fast"]["tier"], "fast")
        self.assertEqual(tasks["slow"]["tier"], "slow")

    def test_discriminating_requires_a_live_band(self):
        tasks = {"live": {"p90": {}, "band": {"swe-2": 0.5}}, "dead": {"p90": {}, "band": {"swe-2": 1.0}}}
        calibrate.classify(tasks, 6.0)
        self.assertTrue(tasks["live"]["discriminates"])
        self.assertFalse(tasks["dead"]["discriminates"])


class VerdictTest(unittest.TestCase):
    def test_refuses_a_winner_on_two_shared_tasks(self):
        per_task = {"droid": {"a": 1.0, "b": 1.0}, "amp": {"a": 0.0, "b": 0.0}}
        winner, reason, *_ = report.verdict(per_task, ("droid", "amp"), RULE)
        self.assertEqual(winner, "insufficient")
        self.assertIn("2 shared task", reason)

    def test_small_gap_is_a_tie_even_when_one_side_wins_every_task(self):
        per_task = {
            "droid": {name: 1.0 for name in "abcde"},
            "amp": {name: 0.98 for name in "abcde"},
        }
        winner, reason, *_ = report.verdict(per_task, ("droid", "amp"), RULE)
        self.assertEqual(winner, "tie")
        self.assertIn("no measurable difference", reason)

    def test_six_discordant_tasks_declare_a_winner(self):
        per_task = {
            "droid": {name: 1.0 for name in "abcdef"},
            "amp": {name: 0.0 for name in "abcdef"},
        }
        winner, reason, *_ = report.verdict(per_task, ("droid", "amp"), RULE)
        self.assertEqual(winner, "droid")
        self.assertIn("6 of 6", reason)

    def test_gap_clears_the_rule_with_enough_tasks(self):
        per_task = {"droid": {"a": 1.0, "b": 1.0, "c": 1.0}, "amp": {"a": 0.5, "b": 0.5, "c": 0.5}}
        winner, reason, *_ = report.verdict(per_task, ("droid", "amp"), RULE)
        self.assertEqual(winner, "droid")
        self.assertIn("clears", reason)


class ModelRegistryTest(unittest.TestCase):
    def test_prints_the_value_the_shell_needs(self):
        import contextlib, io

        for key, field, expected in (
            ("swe-2-max", "route", "devin/swe-2-max"),
            ("swe-2-max", "effort", "max"),
            ("swe-2-1-xhigh", "route", "devin/swe-2-1-xhigh"),
            ("swe-2-1-xhigh", "effort", "xhigh"),
            ("swe-2-1-max", "route", "devin/swe-2-1-max"),
            ("swe-2-1-max", "effort", "max"),
            ("deepseek-engy-max", "route", "engy/deepseek-v4.1-flash-1017(max)"),
        ):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                self.assertEqual(model_id.main(["model_id.py", key, field]), 0)
            self.assertEqual(buffer.getvalue().strip(), expected)

    def test_rejects_an_unknown_key(self):
        import contextlib, io

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(model_id.main(["model_id.py", "nope", "route"]), 2)

    def test_supports_tells_the_matrix_which_harnesses_reach_a_model(self):
        # Each harness reaches Anthropic through its own access, so the registry,
        # not the harness default route, decides whether a pair may run.
        cases = (
            # Retired in favor of haiku-5.5-max/-xhigh: it resolves for archived
            # trials but no harness may launch it now.
            ("haiku-anthropic-direct", "claude", 1),
            ("haiku-5.5-max", "claude", 0),
            ("haiku-5.5-xhigh", "claude", 0),
            ("haiku-5.5-max", "amp", 1),
            ("haiku-amp-native", "amp", 0),
            ("haiku-amp-native", "claude", 1),
            ("deepseek-engy-max", "droid", 0),
            ("deepseek-engy-max", "amp", 0),
            ("deepseek-engy-max", "claude", 0),
            # Droid reaches Haiku through the Devin bridge now that quota is
            # back; other harnesses still can't run it.
            ("haiku-devin-max", "droid", 0),
            ("haiku-devin-max", "amp", 1),
            # swe-2.1 runs on all four harnesses through the Devin bridge.
            ("swe-2-1-xhigh", "droid", 0),
            ("swe-2-1-xhigh", "amp", 0),
            ("swe-2-1-xhigh", "claude", 0),
            ("swe-2-1-xhigh", "codex", 0),
            ("swe-2-1-max", "droid", 0),
            ("swe-2-1-max", "amp", 0),
            ("swe-2-1-max", "claude", 0),
            ("swe-2-1-max", "codex", 0),
        )
        for key, harness, expected in cases:
            with self.subTest(key=key, harness=harness):
                self.assertEqual(
                    model_id.main(["model_id.py", key, "supports", harness]), expected
                )

    def test_supports_rejects_an_unknown_key(self):
        import contextlib, io

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(model_id.main(["model_id.py", "nope", "supports", "claude"]), 2)


class CostTest(unittest.TestCase):
    def test_prices_fresh_cached_and_output_tokens(self):
        models = lib.load_config("models")
        priced = lib.Trial(
            job="job",
            harness="droid",
            model="devin/swe-2-max",
            task="t",
            reward=1.0,
            timed_out=False,
            duration_min=3.0,
            finished=True,
            tokens_in=1_000_000,
            tokens_cached=500_000,
            tokens_out=100_000,
        )
        # 0.5M fresh at 3.00, 0.5M cached at 0.30, 0.1M out at 15.00. The live
        # registry has swe-2-max inside a free window, so strip it for the
        # arithmetic check rather than depend on the calendar.
        models["swe-2-max"]["pricing"].pop("free_until", None)
        self.assertAlmostEqual(lib.list_cost(priced, models), 3.15, places=4)

    def test_free_window_makes_the_real_cost_zero(self):
        models = lib.load_config("models")
        models["swe-2-max"]["pricing"]["free_until"] = "2999-01-01T00:00:00Z"
        trial = lib.Trial(
            job="job", harness="droid", model="devin/swe-2-max", task="t",
            reward=1.0, timed_out=False, duration_min=3.0, finished=True,
            tokens_in=1_000_000, tokens_cached=500_000, tokens_out=100_000,
        )
        self.assertEqual(lib.list_cost(trial, models), 0.0)

    def test_expired_free_window_prices_normally(self):
        models = lib.load_config("models")
        models["swe-2-max"]["pricing"]["free_until"] = "2000-01-01T00:00:00Z"
        trial = lib.Trial(
            job="job", harness="droid", model="devin/swe-2-max", task="t",
            reward=1.0, timed_out=False, duration_min=3.0, finished=True,
            tokens_in=1_000_000, tokens_cached=500_000, tokens_out=100_000,
        )
        self.assertAlmostEqual(lib.list_cost(trial, models), 3.15, places=4)

    def test_unpriced_or_unmetered_trials_return_none(self):
        models = lib.load_config("models")
        unmetered = lib.Trial("job", "droid", "devin/swe-2-max", "t", 1.0, False, 3.0, True)
        self.assertIsNone(lib.list_cost(unmetered, models))


class ScreenTest(unittest.TestCase):
    def test_a_task_without_a_dockerfile_never_reaches_an_agent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(screen.screen(Path(tmp), timeout=1)["status"], "missing-dockerfile")


class PlanTest(unittest.TestCase):
    def run_plan(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = plan.main(argv)
        return code, out.getvalue()

    def test_rejects_a_route_that_does_not_exist_for_the_harness(self):
        code, out = self.run_plan(["--models", "haiku-devin-max", "--harnesses", "claude", "--tasks", "regex-log"])
        self.assertEqual(code, 2)
        self.assertIn("not supported on claude", out)

    def test_builds_a_command_with_the_narrowest_cap(self):
        code, out = self.run_plan(["--models", "swe-2-max", "--harnesses", "droid,amp", "--tasks", "regex-log", "--attempts", "2"])
        self.assertEqual(code, 0)
        self.assertIn("CONCURRENCY=2", out)
        self.assertIn("ATTEMPTS=2", out)
        self.assertIn("TASKS=regex-log", out)

    def test_selection_comes_from_the_manifest(self):
        manifest = json.loads((Path(__file__).resolve().parent.parent / "bench" / "config" / "tasks.json").read_text())
        expected = sorted(
            name
            for name, entry in manifest["tasks"].items()
            if entry.get("tier") == "fast" and entry.get("discriminates")
        )
        code, out = self.run_plan(["--models", "swe-2-max", "--tier", "fast", "--discriminating"])
        if not expected:
            self.assertEqual(code, 2)
            self.assertIn("no tasks selected", out)
            return
        self.assertEqual(code, 0)
        self.assertIn(f"tasks ({len(expected)})", out)


if __name__ == "__main__":
    unittest.main()
