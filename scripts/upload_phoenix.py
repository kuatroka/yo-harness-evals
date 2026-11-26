#!/usr/bin/env python3
"""Upload a completed Harbor job to Phoenix.

The Phoenix Harbor plugin normally records trials while a job is running. This
script rebuilds Harbor's ``Job`` object from an existing job directory so the
same plugin can record the completed trials afterwards.

Usage:
    .venv/bin/python scripts/upload_phoenix.py jobs/<job-name>
    .venv/bin/python scripts/upload_phoenix.py jobs/<job-name> --dry-run

Phoenix connection settings come from the environment, optionally loaded from
``.env``. Set either ``PHOENIX_ENDPOINT`` or ``PHOENIX_COLLECTOR_ENDPOINT`` to
the Phoenix base URL. For Phoenix Cloud that includes the workspace path, for
example ``https://app.phoenix.arize.com/s/<handle>``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from harbor.job import Job
from harbor.models.job.config import JobConfig
from harbor.models.job.result import JobResult
from phoenix.client.harbor import PhoenixJobPlugin
from phoenix.client.harbor._adapter import build_job_plan

_ENV_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$"
)
_SECRET_ENV_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "HEADERS")


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines without requiring an external dotenv package.

    Invalid lines (for example a copied ``Authorization:`` header) are ignored.
    Existing process environment values win, matching dotenv behavior.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(errors="replace").splitlines():
        match = _ENV_ASSIGNMENT.match(raw_line)
        if match is None:
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def endpoint_from_env() -> str | None:
    return os.environ.get("PHOENIX_ENDPOINT") or os.environ.get(
        "PHOENIX_COLLECTOR_ENDPOINT"
    )


def redact(key: str, value: str) -> str:
    if any(marker in key.upper() for marker in _SECRET_ENV_MARKERS):
        return f"<redacted:{len(value)} chars>"
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "job_dir",
        type=Path,
        help="Completed Harbor job directory containing config.json and result.json",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Environment file to load before reading Phoenix settings (default: .env)",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="Override the Phoenix dataset name",
    )
    parser.add_argument(
        "--experiment-name-template",
        default=None,
        help="Optional Phoenix experiment name template",
    )
    parser.add_argument(
        "--trace-mode",
        choices=("atif", "none"),
        default="atif",
        help="Record ATIF trajectories as traces, or disable tracing (default: atif)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load the job and print the Phoenix plan without sending anything",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging from Harbor and Phoenix",
    )
    return parser.parse_args()


_TRIAL_ONLY_KEYS = {"task", "trial_name", "trials_dir", "job_id", "source_trial"}


def trial_configs(job_dir: Path) -> list[dict[str, Any]]:
    configs = []
    for trial in sorted(job_dir.iterdir()):
        path = trial / "config.json"
        if trial.is_dir() and path.is_file():
            configs.append(json.loads(path.read_text()))
    return configs


def unanimous_agent(configs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the single agent config every trial recorded, if they agree."""
    variants = {json.dumps(config.get("agent"), sort_keys=True) for config in configs}
    if len(variants) != 1:
        return None
    return json.loads(next(iter(variants)))


def reconcile_single_agent(config: JobConfig, configs: list[dict[str, Any]]) -> str | None:
    """Adopt the agent config the finished trials recorded when the job's differs.

    Harbor rebuilds planned trial configs from the job's ``config.json`` and
    refuses to load the job when a finished trial recorded something else. A
    trial replaced after a launcher rule change (an added ``--ak`` kwarg, for
    example) keeps the newer agent config, so the job config stops describing
    what ran. When every trial agrees on one agent, that agent is what ran.

    Only single-agent jobs are reconciled; a multi-agent job would need the
    trials grouped back to their owning agent first.
    """
    if len(config.agents) != 1:
        return None
    recorded = unanimous_agent(configs)
    if recorded is None:
        return None
    planned = config.agents[0].model_dump(mode="json")
    if recorded == planned:
        return None
    config.agents[0] = type(config.agents[0]).model_validate(recorded)
    return f"reconciled agent config from trials: kwargs={recorded.get('kwargs')}"


def synthesize_config(job_dir: Path, configs: list[dict[str, Any]]) -> JobConfig:
    """Rebuild a missing job config from what the trials recorded.

    A job assembled from rerun trials can lose its ``config.json``; the trials
    still carry every field Harbor needs to plan the same job again.
    """
    if not configs:
        raise SystemExit(f"No trial configs found under {job_dir}")
    agent = unanimous_agent(configs)
    if agent is None:
        raise SystemExit(
            f"{job_dir.name} has no job config and its trials disagree on the agent"
        )
    task_paths = [Path(str(config["task"]["path"])) for config in configs]
    # Count finished trial directories, not trial configs: a trial whose config
    # went missing still holds a result and needs a planned slot to match.
    finished = [
        trial.name.rsplit("__", 1)[0]
        for trial in job_dir.iterdir()
        if trial.is_dir() and (trial / "result.json").is_file()
    ]
    per_task = Counter(finished) or Counter(path.name for path in task_paths)
    fields = {
        key: value
        for key, value in configs[0].items()
        if key not in _TRIAL_ONLY_KEYS and key != "agent"
    }
    fields.update(
        {
            "job_name": job_dir.name,
            "jobs_dir": job_dir.parent,
            "n_attempts": max(per_task.values()),
            "agents": [agent],
            "datasets": [
                {"path": task_paths[0].parent, "task_names": sorted(per_task)}
            ],
            "tasks": [],
        }
    )
    return JobConfig.model_validate(fields)


def synthesize_trial_config(trial: Path, siblings: list[dict[str, Any]]) -> dict[str, Any]:
    """Rebuild a missing trial config from a sibling that ran the same task."""
    task = trial.name.rsplit("__", 1)[0]
    match = next(
        (c for c in siblings if Path(str(c["task"]["path"])).name == task),
        siblings[0],
    )
    clone = json.loads(json.dumps(match))
    clone["task"]["path"] = str(Path(str(match["task"]["path"])).parent / task)
    clone["trial_name"] = trial.name
    clone["trials_dir"] = str(trial.parent)
    return clone


def stage_mirror(job_dir: Path, config: JobConfig) -> Path:
    """Materialize a corrected copy of the job so the originals stay untouched.

    Harbor re-reads ``config.json`` from disk and compares it with the config it
    was given, so the correction has to exist on disk somewhere. Trial entries
    are symlinked into the copy, which costs nothing and leaves the real job
    intact, while a trial config that went missing can be filled in for real.
    """
    configs = trial_configs(job_dir)
    root = Path(tempfile.mkdtemp(prefix=f"phoenix-{job_dir.name}-"))
    mirror = root / job_dir.name
    mirror.mkdir()
    for trial in job_dir.iterdir():
        if not trial.is_dir():
            continue
        target = mirror / trial.name
        target.mkdir()
        for entry in trial.iterdir():
            if entry.name == "config.json" and not entry.is_file():
                continue
            (target / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
        if not (trial / "config.json").is_file():
            filled = synthesize_trial_config(trial, configs)
            filled["trials_dir"] = str(mirror)
            (target / "config.json").write_text(json.dumps(filled, indent=4))
    shutil.copy2(job_dir / "result.json", mirror / "result.json")
    config.jobs_dir = root
    config.job_name = job_dir.name
    (mirror / "config.json").write_text(config.model_dump_json(indent=4))
    return root


async def upload(args: argparse.Namespace) -> int:
    job_dir = args.job_dir.expanduser().resolve()
    config_path = job_dir / "config.json"
    result_path = job_dir / "result.json"
    if not result_path.exists():
        raise SystemExit(f"Missing Harbor result: {result_path}")

    configs = trial_configs(job_dir)
    staged: Path | None = None
    if config_path.exists():
        config = JobConfig.model_validate_json(config_path.read_text())
        # A moved job directory should still resolve against its current parent.
        config.jobs_dir = job_dir.parent
        config.job_name = job_dir.name
        reconciled = reconcile_single_agent(config, configs)
        if reconciled:
            print(reconciled)
            staged = stage_mirror(job_dir, config)
    else:
        print("rebuilt missing job config from trial configs")
        config = synthesize_config(job_dir, configs)
        staged = stage_mirror(job_dir, config)

    try:
        return await record(job_dir, config, args)
    finally:
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)


async def record(job_dir: Path, config: JobConfig, args: argparse.Namespace) -> int:
    result_path = job_dir / "result.json"
    job = await Job.create(config)
    trial_results = list(job._existing_trial_results)
    if not trial_results:
        raise SystemExit(f"No completed trials found under {job_dir}")

    plan = build_job_plan(job, dataset_override=args.dataset)
    print(f"job:        {plan.job_id}")
    print(f"dataset:    {plan.dataset.name}")
    print(f"tasks:      {len(plan.tasks)}")
    print(f"experiments:{len(plan.slices)}")
    print(f"trials:     {len(trial_results)}")
    print(f"trace_mode: {args.trace_mode}")
    if args.dry_run:
        print("dry run:    no data sent")
        return 0

    endpoint = endpoint_from_env()
    api_key = os.environ.get("PHOENIX_API_KEY")
    if not endpoint:
        raise SystemExit(
            "Set PHOENIX_ENDPOINT or PHOENIX_COLLECTOR_ENDPOINT. "
            "For Phoenix Cloud use the console base URL, for example "
            "https://app.phoenix.arize.com/s/<handle>."
        )
    if not api_key:
        raise SystemExit("Set PHOENIX_API_KEY in the environment or .env file")
    print(f"endpoint:   {endpoint}")
    print(f"api_key:    {redact('PHOENIX_API_KEY', api_key)}")

    plugin = PhoenixJobPlugin(
        dataset=args.dataset,
        endpoint=endpoint,
        api_key=api_key,
        trace_mode=None if args.trace_mode == "none" else "atif",
        experiment_name_template=args.experiment_name_template,
    )
    await plugin.on_job_start(job)
    await plugin.on_job_end(JobResult.model_validate_json(result_path.read_text()))

    if plugin.plan is None:
        raise SystemExit("Phoenix plugin did not initialize a job plan")
    print(
        "recorded:   "
        f"{len(plugin.experiments)} experiment(s), "
        f"{len(plugin._runs)} run(s)"
    )
    return 0


def main() -> int:
    args = parse_args()
    load_env_file(args.env_file)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(upload(args))


if __name__ == "__main__":
    raise SystemExit(main())
