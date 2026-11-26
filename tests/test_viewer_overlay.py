"""Behaviour of the launch overlay: what a caller can observe.

Each test drives a public entry point: the resolver, the runner's process and
record, the middleware's bytes, the built static directory, or the HTTP routes.
Nothing here reads a private helper.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from viewer_overlay import app as overlay_app
from viewer_overlay import columns, harbor_api, launch, registry, runner, static_build
from viewer_overlay.middleware import OverlayJSONMiddleware, eligible

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_JOB = REPO_ROOT / "jobs" / "verify-codexfix-codex-swe-2-1-max-v1"


def make_dataset(root: Path) -> Path:
    """One dataset with one task, in the layout run_matrix.sh expects."""
    task = root / "frontier-harness-eval" / "wal-recovery-ordering"
    task.mkdir(parents=True, exist_ok=True)
    return root


class ValidationTest(unittest.TestCase):
    """The boundary refuses what the runner would otherwise run anyway."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.datasets = make_dataset(Path(self.temp.name) / "datasets")
        self.reg = registry.load()

    def resolve(self, **overrides: Any) -> launch.Resolution:
        candidates: dict[str, Any] = {
            "agent": "codex",
            "model": "9r/dv-bridge/swe-2-1-max",
            "effort": "max",
            "tasks": ["wal-recovery-ordering"],
        }
        candidates.update(overrides)
        return launch.resolve(candidates, self.reg, self.datasets)

    def assertProblem(self, resolution: launch.Resolution, field: str, needle: str) -> None:
        self.assertIsNone(resolution.spec)
        matches = [p for p in resolution.unresolved if p.startswith(f"{field}:")]
        self.assertTrue(matches, f"no {field} problem in {resolution.unresolved}")
        self.assertTrue(any(needle in problem for problem in matches), matches)

    def test_a_good_spec_resolves(self):
        resolution = self.resolve()
        self.assertEqual(resolution.unresolved, ())
        assert resolution.spec is not None
        self.assertEqual(resolution.spec.tasks, ("wal-recovery-ordering",))
        self.assertEqual(resolution.spec.dataset, "frontier-harness-eval")
        self.assertEqual(resolution.spec.attempts, 2)
        self.assertEqual(resolution.spec.retries, 2)
        self.assertEqual(resolution.spec.n_concurrent, 1)

    def test_unknown_agent(self):
        self.assertProblem(self.resolve(agent="nope"), "agent", "nope")

    def test_unknown_model(self):
        self.assertProblem(self.resolve(model="gpt-99"), "model", "gpt-99")

    def test_model_that_does_not_route_the_agent(self):
        # The Devin Haiku row is droid-only in the registry.
        self.assertProblem(
            self.resolve(agent="claude", model="9r/dv-bridge/haiku-5-5-max"),
            "model",
            "route",
        )

    def test_unknown_effort(self):
        self.assertProblem(self.resolve(effort="turbo"), "effort", "turbo")

    def test_task_missing_from_the_dataset(self):
        resolution = self.resolve(tasks=["wal-recovery-ordering", "not-a-task"])
        self.assertProblem(resolution, "task", "not-a-task")
        self.assertEqual(resolution.partial["tasks"], ["wal-recovery-ordering", "not-a-task"])

    def test_no_tasks(self):
        self.assertProblem(self.resolve(tasks=[]), "tasks", "missing")


class RerunTest(unittest.TestCase):
    """A rerun derives its fields from the job's own records."""

    def setUp(self) -> None:
        self.reg = registry.load()
        self.datasets = REPO_ROOT / "datasets"

    def test_real_job(self):
        self.assertTrue(REAL_JOB.is_dir(), f"missing fixture job: {REAL_JOB}")
        resolution = launch.resolve_from_job(REAL_JOB, self.reg, self.datasets, REAL_JOB.name)
        self.assertEqual(resolution.unresolved, ())
        assert resolution.spec is not None
        spec = resolution.spec
        self.assertEqual(spec.agent, "codex")
        self.assertEqual(spec.tasks, ("wal-recovery-ordering",))
        self.assertEqual(spec.effort, "max")
        self.assertEqual(spec.from_job, REAL_JOB.name)
        entry = self.reg.get(spec.model)
        assert entry is not None
        self.assertEqual(entry.route, "devin/swe-2-1-max")

    def test_job_from_a_second_dataset_keeps_its_dataset(self):
        """A bench-fast job names tasks the default dataset does not have."""
        job = REPO_ROOT / "jobs" / "2026-11-25__19-53-25"
        self.assertTrue(job.is_dir(), f"missing fixture job: {job}")
        resolution = launch.resolve_from_job(job, self.reg, self.datasets, job.name)
        self.assertEqual(resolution.unresolved, ())
        assert resolution.spec is not None
        self.assertEqual(resolution.spec.dataset, "bench-fast")
        self.assertEqual(resolution.spec.tasks, ("burntsushi__ripgrep-2555",))

    def test_unreadable_config_is_named_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "broken-job"
            job.mkdir()
            resolution = launch.resolve_from_job(job, self.reg, self.datasets, job.name)
        self.assertIsNone(resolution.spec)
        self.assertTrue(any(p.startswith("job:") for p in resolution.unresolved))
        self.assertTrue(any(p.startswith("agent:") for p in resolution.unresolved))

    def test_unknown_records_are_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "old-job"
            job.mkdir()
            (job / "config.json").write_text(
                json.dumps(
                    {
                        "agents": [{"name": "harnesses.mystery:Mystery", "model_name": "x/y"}],
                        "datasets": [{"task_names": ["wal-recovery-ordering"]}],
                    }
                )
            )
            resolution = launch.resolve_from_job(job, self.reg, self.datasets, job.name)
        self.assertIsNone(resolution.spec)
        self.assertTrue(any(p.startswith("agent:") for p in resolution.unresolved))
        self.assertTrue(any(p.startswith("model:") for p in resolution.unresolved))
        self.assertEqual(resolution.partial["tasks"], ["wal-recovery-ordering"])


    def test_legacy_dv_route_still_names_its_model(self):
        """Job configs from 18 November spell the Devin route ``dv/``."""
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "legacy-job"
            job.mkdir()
            (job / "config.json").write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "name": "harnesses.droid:DroidAgent",
                                "model_name": "dv/swe-2-max",
                                "kwargs": {"reasoning_effort": "max"},
                            }
                        ],
                        "datasets": [
                            {
                                "path": str(self.datasets / "frontier-harness-eval"),
                                "task_names": ["wal-recovery-ordering"],
                            }
                        ],
                    }
                )
            )
            resolution = launch.resolve_from_job(job, self.reg, self.datasets, job.name)
        self.assertEqual(resolution.unresolved, ())
        assert resolution.spec is not None
        self.assertEqual(
            resolution.spec.model, "9r/dv-bridge/swe-2-max"
        )
        entry = self.reg.get("9r/dv-bridge/swe-2-max")
        assert entry is not None
        self.assertEqual(entry.route, "devin/swe-2-max")

    def test_a_recorded_model_id_beats_the_shared_route(self):
        """A native Claude id carries no effort, so the stored id must win."""
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "recorded-id-job"
            job.mkdir()
            (job / "config.json").write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "name": "harnesses.claude:ClaudeCodeAgent",
                                "model_name": "claude-haiku-5-5",
                                "kwargs": {
                                    "model_id": "native/claude/haiku-5-5-xhigh"
                                },
                            }
                        ],
                        "datasets": [
                            {
                                "path": str(self.datasets / "frontier-harness-eval"),
                                "task_names": ["wal-recovery-ordering"],
                            }
                        ],
                    }
                )
            )
            resolution = launch.resolve_from_job(job, self.reg, self.datasets, job.name)
        self.assertEqual(resolution.unresolved, ())
        assert resolution.spec is not None
        self.assertEqual(resolution.spec.model, "native/claude/haiku-5-5-xhigh")
        self.assertEqual(resolution.spec.effort, "xhigh")


class RegistryTest(unittest.TestCase):
    """bench/model_id.py and the overlay read one file, so they must agree."""

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "bench" / "model_id.py"), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_keys_routes_efforts_and_support_match_the_cli(self):
        reg = registry.load()
        keys = self.cli("keys").stdout.strip().split(",")
        self.assertEqual(sorted(keys), sorted(reg.keys()))
        efforts = self.cli("efforts").stdout.strip().split(",")
        self.assertEqual(efforts, list(reg.efforts()))
        for key in keys:
            entry = reg.get(key)
            assert entry is not None
            self.assertEqual(self.cli(key, "route").stdout.strip(), entry.route)
            self.assertEqual(self.cli(key, "effort").stdout.strip(), entry.effort)
        for key, agent in (
            ("9r/dv-bridge/swe-2-1-max", "codex"),
            ("9r/dv-bridge/haiku-5-5-max", "claude"),
        ):
            supported = self.cli(key, "supports", agent).returncode == 0
            self.assertEqual(supported, reg.supports(key, agent))

    def test_agents_are_the_sorted_union_with_labels(self):
        agents = registry.load().agents()
        self.assertEqual([agent for agent, _ in agents], ["amp", "claude", "codex", "droid"])
        self.assertEqual(dict(agents)["claude"], "Claude Code")


class RunnerTest(unittest.TestCase):
    """The matrix gets the environment the spec describes."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_dataset(self.root / "datasets")
        self.spec = launch.LaunchSpec(
            agent="codex",
            model="9r/dv-bridge/swe-2-1-max",
            effort="max",
            tasks=("wal-recovery-ordering", "adaptive-rejection-sampler"),
            n_concurrent=3,
            attempts=4,
            retries=1,
        )

    def test_dry_run_writes_the_environment_and_spawns_nothing(self):
        with mock.patch("viewer_overlay.runner.subprocess.Popen") as popen:
            started = runner.start(self.spec, repo_root=self.root, dry_run=True)
            popen.assert_not_called()

        self.assertFalse(started.spawned)
        self.assertEqual(started.job_prefix, f"ui-{started.launch_id}")
        log = started.log_path.read_text(encoding="utf-8")
        expected = [
            f"JOB_PREFIX=ui-{started.launch_id}",
            "HARNESSES=codex",
            "MODELS=9r/dv-bridge/swe-2-1-max",
            "EFFORTS=max",
            "TASKS=wal-recovery-ordering,adaptive-rejection-sampler",
            "ATTEMPTS=4",
            "RETRIES=1",
            "CONCURRENCY=3",
            f"DATASET={self.root / 'datasets' / 'frontier-harness-eval'}",
            f"JOBS_DIR={self.root / 'jobs'}",
            f"LOG_DIR={self.root / 'logs'}",
            "command: bash scripts/run_matrix.sh",
        ]
        for line in expected:
            self.assertIn(line, log)
        self.assertIn("RUN_ID=", log)

        record = json.loads(started.record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["spec"], self.spec.as_dict())
        self.assertEqual(record["job_prefix"], started.job_prefix)
        self.assertTrue(record["dry_run"])

    def test_a_real_start_hands_the_matrix_its_environment(self):
        with mock.patch("viewer_overlay.runner.subprocess.Popen") as popen:
            started = runner.start(self.spec, repo_root=self.root, dry_run=False)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(command, ["bash", "scripts/run_matrix.sh"])
        self.assertEqual(kwargs["cwd"], str(self.root))
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(kwargs["env"]["JOB_PREFIX"], started.job_prefix)
        self.assertEqual(kwargs["env"]["MODELS"], self.spec.model)
        self.assertEqual(kwargs["env"]["TASKS"], "wal-recovery-ordering,adaptive-rejection-sampler")
        self.assertTrue(started.spawned)

    def test_a_launch_record_is_written_once(self):
        path = self.root / "logs" / "launches" / "same-id.json"
        self.assertTrue(runner.write_record(path, {"launch_id": "same-id", "attempt": 1}))
        self.assertFalse(runner.write_record(path, {"launch_id": "same-id", "attempt": 2}))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["attempt"], 1)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["same-id.json"])


class StatusTest(unittest.TestCase):
    """Status is whatever the job folders say on this request."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "jobs").mkdir()
        (self.root / "logs" / "launches").mkdir(parents=True)

    def job(self, name: str, result: dict[str, Any] | None) -> None:
        job = self.root / "jobs" / name
        job.mkdir()
        if result is not None:
            (job / "result.json").write_text(json.dumps(result), encoding="utf-8")

    def record(self, launch_id: str, prefix: str, created_at: str) -> None:
        (self.root / "logs" / "launches" / f"{launch_id}.json").write_text(
            json.dumps(
                {
                    "launch_id": launch_id,
                    "created_at": created_at,
                    "spec": {"agent": "codex", "tasks": []},
                    "job_prefix": prefix,
                }
            ),
            encoding="utf-8",
        )

    def test_job_states_and_launch_status(self):
        self.job("ui-a-live", None)
        self.job("ui-a-done", {"finished_at": "2026-01-01T00:00:00Z", "stats": {"n_errored_trials": 0}})
        self.job("ui-a-bad", {"finished_at": "2026-01-01T00:00:00Z", "stats": {"n_errored_trials": 2}})
        self.job("ui-b-done", {"finished_at": "2026-01-01T00:00:00Z", "stats": {"n_errored_trials": 0}})
        self.record("a", "ui-a", "2026-01-01T00:00:00Z")
        self.record("b", "ui-b", "2026-01-02T00:00:00Z")

        launches = runner.list_launches(repo_root=self.root)
        by_id = {entry["launch_id"]: entry for entry in launches}
        self.assertEqual([entry["launch_id"] for entry in launches], ["b", "a"])
        self.assertEqual(by_id["a"]["status"], "running")
        self.assertEqual(
            {job["job_name"]: job["state"] for job in by_id["a"]["jobs"]},
            {"ui-a-live": "running", "ui-a-done": "completed", "ui-a-bad": "failed"},
        )
        self.assertEqual(by_id["b"]["status"], "completed")

    def test_a_launch_with_no_job_folder_is_pending(self):
        self.record("c", "ui-c", "2026-01-03T00:00:00Z")
        launches = runner.list_launches(repo_root=self.root)
        self.assertEqual(launches[0]["status"], "pending")
        self.assertEqual(launches[0]["jobs"], [])


class JSONApp:
    """A tiny ASGI app: one JSON response, nothing else."""

    def __init__(self, body: bytes, content_type: bytes = b"application/json") -> None:
        self.body = body
        self.content_type = content_type

    async def __call__(self, scope, receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", self.content_type),
                    (b"content-length", str(len(self.body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": self.body})


def run_asgi(app, path: str = "/api/jobs") -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app({"type": "http", "method": "GET", "path": path, "headers": []}, receive, send))
    return sent


def body_of(messages: list[dict[str, Any]]) -> bytes:
    return b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")


class MiddlewareTest(unittest.TestCase):
    """Labels and totals are rewritten once, whatever the payload shape."""

    def payload(self) -> dict[str, Any]:
        return {
            "name": "job",
            "agents": ["harnesses.amp:AmpAgent", "harnesses.claude:ClaudeCodeAgent"],
            "models": ["devin/swe-2-1-max", "engy/deepseek-v4.1-flash-1017(max)"],
            "trials": [
                {
                    "agent_name": "harbor.agents.installed.codex:Codex",
                    "reported_agent_name": "harnesses.amp:AmpAgent",
                    "model_name": "claude-haiku-5-5",
                    "reasoning_effort": "max",
                    "input_tokens": 4,
                    "output_tokens": 5,
                }
            ],
            "total_input_tokens": 10,
            "total_output_tokens": 20,
        }

    def test_one_pass_rewrites_labels_and_totals(self):
        body = json.dumps(self.payload()).encode("utf-8")
        messages = run_asgi(OverlayJSONMiddleware(JSONApp(body)))
        payload = json.loads(body_of(messages))

        self.assertEqual(payload["agents"], ["Amp", "Claude Code"])
        self.assertEqual(payload["models"], ["swe-2-1(max)", "deepseek-v4.1-flash-1017(max)"])
        trial = payload["trials"][0]
        self.assertEqual(trial["agent_name"], "Codex")
        self.assertEqual(trial["reported_agent_name"], "Amp")
        self.assertEqual(trial["model_name"], "claude-haiku-5-5(max)")
        self.assertEqual(trial["total_tokens"], 9)
        self.assertEqual(payload["total_all_tokens"], 30)
        self.assertEqual(
            list(payload),
            [
                "name",
                "agents",
                "models",
                "trials",
                "total_input_tokens",
                "total_output_tokens",
                "total_all_tokens",
            ],
        )
        start = messages[0]
        headers = dict(start["headers"])
        self.assertEqual(int(headers[b"content-length"]), len(body_of(messages)))

    def test_applied_twice_equals_applied_once(self):
        body = json.dumps(self.payload()).encode("utf-8")
        once = run_asgi(OverlayJSONMiddleware(JSONApp(body)))
        twice = run_asgi(OverlayJSONMiddleware(OverlayJSONMiddleware(JSONApp(body))))
        self.assertEqual(body_of(once), body_of(twice))

    def test_a_non_json_response_is_byte_identical(self):
        body = b"<html>not json</html>"
        raw = run_asgi(JSONApp(body, content_type=b"text/html"))
        wrapped = run_asgi(OverlayJSONMiddleware(JSONApp(body, content_type=b"text/html")))
        self.assertEqual(wrapped, raw)

    def test_a_json_body_that_will_not_parse_is_untouched(self):
        raw = run_asgi(JSONApp(b"{oops"))
        wrapped = run_asgi(OverlayJSONMiddleware(JSONApp(b"{oops")))
        self.assertEqual(wrapped, raw)

    def test_stream_paths_are_not_buffered(self):
        self.assertFalse(eligible("/api/jobs/x/stream"))
        self.assertTrue(eligible("/api/jobs"))
        self.assertFalse(eligible("/assets/app.js"))
        body = json.dumps({"agents": ["amp-code"]}).encode("utf-8")
        raw = run_asgi(JSONApp(body), path="/api/jobs/x/stream")
        wrapped = run_asgi(OverlayJSONMiddleware(JSONApp(body)), path="/api/jobs/x/stream")
        self.assertEqual(wrapped, raw)

    def test_a_non_json_response_is_forwarded_before_its_body_arrives(self):
        # Recordings and images must not be held in memory while they stream.
        seen: dict[str, int] = {}
        sent: list[dict[str, Any]] = []

        class Probe:
            def __init__(self, content_type: bytes) -> None:
                self.content_type = content_type

            async def __call__(self, scope, receive, send) -> None:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-type", self.content_type)],
                    }
                )
                seen["after_start"] = len(sent)
                await send({"type": "http.response.body", "body": b"chunk"})

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": "/api/file", "headers": []}
        asyncio.run(OverlayJSONMiddleware(Probe(b"application/octet-stream"))(scope, receive, send))
        self.assertEqual(seen["after_start"], 1)
        self.assertEqual(len(sent), 2)

        sent.clear()
        asyncio.run(OverlayJSONMiddleware(Probe(b"application/json"))(scope, receive, send))
        self.assertEqual(seen["after_start"], 0)


class StaticBuildTest(unittest.TestCase):
    """The built bundle carries the nav once, and rebuilds only when it must."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "static"
        (self.source / "assets").mkdir(parents=True)
        (self.source / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        (self.source / "assets" / "home-TEST.js").write_text(
            '{accessorKey:"total_cost_usd",header:', encoding="utf-8"
        )
        (self.source / "assets" / "rollouts-table-TEST.js").write_text(
            '{accessorKey:"cost_usd",header:', encoding="utf-8"
        )
        (self.source / "index.html").write_text(
            '<html><body><div id="app"></div></body></html>', encoding="utf-8"
        )
        self.target = self.root / ".viewer_static"

    def test_injects_once_and_skips_an_unchanged_build(self):
        first = static_build.build(self.target, self.source)
        self.assertTrue(first.rebuilt)
        html = (self.target / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count(static_build.NAV_TAG), 1)
        self.assertLess(html.index(static_build.NAV_TAG), html.index("</body>"))
        self.assertEqual((self.target / "assets" / "app.js").read_text(encoding="utf-8"), "console.log(1)")

        second = static_build.build(self.target, self.source)
        self.assertFalse(second.rebuilt)

        forced = static_build.build(self.target, self.source, force=True)
        self.assertTrue(forced.rebuilt)
        self.assertEqual(
            (self.target / "index.html").read_text(encoding="utf-8").count(static_build.NAV_TAG), 1
        )

    def test_the_built_copy_gets_the_total_column(self):
        static_build.build(self.target, self.source)
        self.assertEqual(columns.verify(self.target / "assets"), [])
        source_home = (self.source / "assets" / "home-TEST.js").read_text(encoding="utf-8")
        self.assertNotIn('accessorKey:"total_all_tokens"', source_home)

    def test_a_changed_bundle_rebuilds(self):
        static_build.build(self.target, self.source)
        (self.source / "assets" / "app.js").write_text("console.log(2)", encoding="utf-8")
        again = static_build.build(self.target, self.source)
        self.assertTrue(again.rebuilt)
        self.assertEqual(
            (self.target / "assets" / "app.js").read_text(encoding="utf-8"), "console.log(2)"
        )

    def test_a_missing_body_tag_names_the_harbor_version(self):
        (self.source / "index.html").write_text("<html><body>no close</html>", encoding="utf-8")
        with self.assertRaises(static_build.StaticBuildError) as caught:
            static_build.build(self.target, self.source)
        self.assertIn(static_build.harbor_version(), str(caught.exception))
        self.assertIn("</body>", str(caught.exception))


class HarborAdapterTest(unittest.TestCase):
    """The installed Harbor is checked in one place before the app is built."""

    def test_the_installed_harbor_is_supported(self):
        self.assertEqual(harbor_api.ensure_supported(), harbor_api.installed_version())

    def test_a_newer_minor_names_the_fix(self):
        with self.assertRaises(harbor_api.HarborAPIError) as caught:
            harbor_api.ensure_supported("0.27.0")
        self.assertIn("0.26", str(caught.exception))
        self.assertIn("harbor_api.py", str(caught.exception))


class AppRoutesTest(unittest.TestCase):
    """The real app: Harbor's routes, the overlay's routes, one job folder."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.jobs = root / "jobs"
        cls.jobs.mkdir()
        cls.datasets = make_dataset(root / "datasets")
        fixture = cls.jobs / "fixture-job"
        fixture.mkdir()
        (fixture / "config.json").write_text(
            json.dumps(
                {
                    "agents": [
                        {
                            "name": "harbor.agents.installed.codex:Codex",
                            "model_name": "devin/swe-2-1-max",
                            "kwargs": {"reasoning_effort": "max"},
                        }
                    ],
                    "datasets": [{"task_names": ["wal-recovery-ordering"]}],
                }
            ),
            encoding="utf-8",
        )
        cls.app = overlay_app.build_app(cls.jobs, root / ".viewer_static", cls.datasets)
        from fastapi.testclient import TestClient

        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def post_launch(self, body: dict[str, Any]):
        with mock.patch.dict(os.environ, {"OVERLAY_DRY_RUN": "1"}):
            return self.client.post("/api/overlay/launches", json=body)

    def test_the_launch_page_is_served_not_the_spa(self):
        response = self.client.get("/launch")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn("New run", response.text)
        self.assertIn("/launch/launch.js", response.text)
        self.assertNotIn("__reactRouterRouteModules", response.text)

    def test_the_page_assets_are_served(self):
        for path, kind in (
            ("/launch/launch.js", "text/javascript"),
            ("/launch/launch.css", "text/css"),
            ("/overlay/nav.js", "application/javascript"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertTrue(response.headers["content-type"].startswith(kind), (path, response.headers))

    def test_options_lists_agents_models_efforts_and_tasks(self):
        payload = self.client.get("/api/overlay/options").json()
        self.assertEqual([agent["id"] for agent in payload["agents"]], ["amp", "claude", "codex", "droid"])
        self.assertEqual(payload["agents"][1]["label"], "Claude Code")
        self.assertEqual(payload["efforts"], ["high", "max", "xhigh"])
        self.assertEqual(payload["default_dataset"], "frontier-harness-eval")
        self.assertIn("wal-recovery-ordering", payload["datasets"][0]["tasks"])
        self.assertEqual(payload["datasets"][0]["name"], "frontier-harness-eval")
        self.assertTrue(payload["models"])
        for model in payload["models"]:
            self.assertTrue(model["route"], model)
            self.assertTrue(model["provider"], model)
            self.assertTrue(model["label"], model)
            self.assertIn(model["transport"], ("9r", "native", "direct"))
        by_key = {model["key"]: model for model in payload["models"]}
        self.assertEqual(
            sorted(by_key["9r/dv-bridge/swe-2-1-max"]["harnesses"]),
            ["amp", "claude", "codex", "droid"],
        )
        self.assertEqual(
            by_key["9r/dv-bridge/swe-2-1-max"]["label"], "swe-2-1-max"
        )
        # A retired row stays resolvable for archived trials, and routes to nobody.
        self.assertEqual(by_key["9r/dv-bridge/haiku-5-5-xhigh"]["harnesses"], [])
        self.assertIn("9r/dv-bridge/swe-2-1-max", by_key)

    def test_an_unresolvable_launch_answers_409_with_the_partial_spec(self):
        response = self.post_launch(
            {
                "agent": "codex",
                "model": "9r/dv-bridge/swe-2-1-max",
                "effort": "max",
                "tasks": ["no-such-task"],
            }
        )
        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertTrue(any(problem.startswith("task:") for problem in payload["unresolved"]))
        self.assertEqual(payload["partial"]["tasks"], ["no-such-task"])
        self.assertEqual(payload["partial"]["agent"], "codex")

    def test_a_dry_run_launch_is_recorded_and_listed(self):
        before = len(self.client.get("/api/overlay/launches").json())
        response = self.post_launch(
            {
                "agent": "codex",
                "model": "9r/dv-bridge/swe-2-1-max",
                "effort": "max",
                "tasks": ["wal-recovery-ordering"],
                "n_concurrent": 1,
                "attempts": 2,
                "retries": 2,
            }
        )
        self.assertEqual(response.status_code, 201)
        created = response.json()
        self.assertEqual(
            sorted(created), ["job_prefix", "launch_id", "log_path"]
        )
        self.assertEqual(created["job_prefix"], f"ui-{created['launch_id']}")
        log = Path(created["log_path"]).read_text(encoding="utf-8")
        self.assertIn("nothing spawned", log)
        self.assertIn(f"JOB_PREFIX=ui-{created['launch_id']}", log)

        launches = self.client.get("/api/overlay/launches").json()
        self.assertEqual(len(launches), before + 1)
        listed = launches[0]
        self.assertEqual(listed["launch_id"], created["launch_id"])
        self.assertEqual(listed["status"], "pending")
        self.assertEqual(listed["jobs"], [])
        self.assertEqual(
            listed["spec"]["model"], "9r/dv-bridge/swe-2-1-max"
        )
        self.assertEqual(listed["spec"]["tasks"], ["wal-recovery-ordering"])

    def test_a_job_spec_is_derived_from_its_config(self):
        response = self.client.get("/api/overlay/jobs/fixture-job/spec")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["unresolved"], [])
        self.assertEqual(payload["spec"]["agent"], "codex")
        self.assertEqual(
            payload["spec"]["model"], "9r/dv-bridge/swe-2-1-max"
        )
        self.assertEqual(payload["spec"]["tasks"], ["wal-recovery-ordering"])
        self.assertEqual(payload["spec"]["from_job"], "fixture-job")

    def test_unknown_jobs_answer_404(self):
        self.assertEqual(self.client.get("/api/overlay/jobs/nope/spec").status_code, 404)
        self.assertEqual(
            self.post_launch({"from_job": "nope"}).status_code, 409
        )


if __name__ == "__main__":
    unittest.main()
