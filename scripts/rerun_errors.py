#!/usr/bin/env python3
"""Rerun errored trials from the newest job per harness and model.

The bench tasks used to carry a ten-minute agent window. A trial that hit it
finished with ``AgentTimeoutError``, and a killed agent writes no usage event,
so those trials report a clock and a blank token count instead of a model
result. This finds, for each harness and model, the newest finished job that
covers every requested task, reruns the trials in it that errored or overran
their budget, and uses ``replace_trial.py`` to put each rerun in the slot it
corrects.

Every rerun is a fresh one-task job with no retries, so one replacement maps to
exactly one stale trial. The rerun job moves to ``archive/reruns`` afterwards,
which keeps it out of the aggregates that read every job under ``jobs/``.

Usage:
    python3 scripts/rerun_errors.py --dry-run
    python3 scripts/rerun_errors.py
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))

import lib  # noqa: E402

DEFAULT_TASKS = "alacritty__alacritty-3921,burntsushi__ripgrep-2555"
DEFAULT_MODELS = (
    "9r/dv-bridge/swe-2-max,"
    "9r/dv-bridge/swe-2-flash-max,"
    "9r/engy/deepseek-4-1-flash-1017-max"
)
DEFAULT_HARNESSES = "droid,amp,claude"
ARCHIVE = ROOT / "archive" / "reruns"


def split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def read_jobs(models: dict) -> dict[str, dict]:
    """Every finished job with the harness, model and tasks of each trial."""
    jobs: dict[str, dict] = {}
    for job in sorted((ROOT / "jobs").iterdir()):
        summary_path = job / "result.json"
        if not summary_path.is_file():
            continue
        summary = json.loads(summary_path.read_text())
        if not summary.get("finished_at"):
            continue
        trials = []
        for result_path in job.glob("*/result.json"):
            data = json.loads(result_path.read_text())
            config_agent = (data.get("config") or {}).get("agent") or {}
            agent_info = data.get("agent_info") or {}
            kwargs = config_agent.get("kwargs") or {}
            if not isinstance(kwargs, dict):
                kwargs = {}
            harness = lib.HARNESS_ALIASES.get(
                agent_info.get("name")
            ) or lib.HARNESS_ALIASES.get(config_agent.get("name"))
            observed = (agent_info.get("model_info") or {}).get(
                "name"
            ) or config_agent.get("model_name")
            effort = kwargs.get("effort") or kwargs.get("reasoning_effort")
            recorded_id = kwargs.get("model_id")
            trials.append(
                {
                    "dir": result_path.parent.name,
                    "task": (data.get("task_name") or "").split("/")[-1],
                    "harness": harness,
                    "model": (
                        recorded_id
                        if isinstance(recorded_id, str) and recorded_id in models
                        else lib.resolve_model(
                            observed,
                            models,
                            effort if isinstance(effort, str) else None,
                        )
                    ),
                    "exception": (data.get("exception_info") or {}).get(
                        "exception_type"
                    ),
                    "over_budget": lib.ran_over_budget(data),
                }
            )
        jobs[job.name] = {"finished": summary["finished_at"], "trials": trials}
    return jobs


def latest_job(jobs: dict[str, dict], harness: str, model: str, tasks: list[str]) -> dict | None:
    """Newest finished job for the pair that contains every requested task."""
    candidates = []
    for name, job in jobs.items():
        pair = [t for t in job["trials"] if t["harness"] == harness and t["model"] == model]
        if {t["task"] for t in pair} >= set(tasks):
            candidates.append((job["finished"], name, pair))
    if not candidates:
        return None
    _, name, pair = max(candidates)
    return {"name": name, "trials": pair}


def rerun(harness: str, model: str, task: str, *, dry_run: bool) -> Path | None:
    run_id = f"rerun-{harness}-{lib.slug(model)}-{datetime.datetime.now():%H%M%S}"
    env = dict(os.environ)
    env.update(
        DATASET=str(ROOT / "datasets" / "bench-fast"),
        TASKS=task,
        HARNESSES=harness,
        MODELS=model,
        ATTEMPTS="1",
        # A rerun that hits a retryable provider error should get the retry
        # Harbor's policy allows; replace() picks the finished attempt.
        RETRIES="1",
        CONCURRENCY="1",
        RUN_ID=run_id,
    )
    if dry_run:
        print(f"  would rerun {harness} {model} {task} as {run_id}")
        return None
    print(f"  rerun {harness} {model} {task} as {run_id}", flush=True)
    subprocess.run(["bash", str(ROOT / "scripts" / "run_matrix.sh")], env=env, cwd=ROOT)
    matches = sorted(
        (p for p in (ROOT / "jobs").glob(f"*{run_id}") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    return matches[-1] if matches else None


def replace(original: str, old_name: str, job: Path, task: str, *, dry_run: bool) -> bool:
    results = []
    for path in job.iterdir():
        result_path = path / "result.json"
        if not result_path.is_file():
            continue
        data = json.loads(result_path.read_text())
        if not data.get("finished_at"):
            continue
        exception = (data.get("exception_info") or {}).get("exception_type")
        results.append((exception is not None, data.get("finished_at"), path, exception))
    if not results:
        print(f"  {job.name}: no finished trial; nothing replaced")
        return False
    # Retries land as sibling attempts. Take the newest one that finished
    # without an error; an errored replacement never replaces the stale trial.
    clean = [row for row in results if not row[0]]
    if not clean:
        exception = results[-1][3]
        print(f"  {job.name}: replacement hit {exception}; keeping the old trial")
        return False
    new_dir = max(clean, key=lambda row: row[1])[2]
    command = [
        sys.executable,
        str(ROOT / "scripts" / "replace_trial.py"),
        "--job",
        str(ROOT / "jobs" / original),
        "--old",
        old_name,
        "--new",
        str(new_dir),
    ]
    if dry_run:
        print("  would run:", " ".join(command[1:]))
        return True
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    print(f"  {old_name} <- {new_dir.name}: exit {result.returncode}")
    if result.returncode != 0:
        print(result.stderr.strip()[:400])
        return False
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    destination = ARCHIVE / job.name
    if destination.exists():
        shutil.rmtree(destination)
    shutil.move(str(job), destination)
    return True


def fix(job_name: str, trial: dict, *, dry_run: bool) -> bool:
    """Rerun one stale trial and put it in the slot it corrects."""
    if not trial["harness"] or not trial["model"]:
        print(f"  {trial['dir']}: no registry row for {trial['harness']} {trial['model']}")
        return False
    reason = trial["exception"] or "over budget"
    print(f"  {trial['task']} {trial['dir']}: {reason}")
    rerun_job = rerun(trial["harness"], trial["model"], trial["task"], dry_run=dry_run)
    if rerun_job is None:
        return dry_run
    return replace(job_name, trial["dir"], rerun_job, trial["task"], dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default=DEFAULT_TASKS)
    parser.add_argument("--models", default=DEFAULT_MODELS)
    parser.add_argument("--harnesses", default=DEFAULT_HARNESSES)
    parser.add_argument("--job", default="", help="fix one job instead of the newest per pair")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    tasks, models, harnesses = split(args.tasks), split(args.models), split(args.harnesses)
    jobs = read_jobs(lib.load_config("models"))
    replaced = failed = 0

    if args.job:
        job = jobs.get(args.job)
        if job is None:
            print(f"no finished job named {args.job}", file=sys.stderr)
            return 2
        stale = [t for t in job["trials"] if t["exception"] or t["over_budget"]]
        print(f"{args.job}: {len(stale)} stale trial(s)")
        for trial in sorted(stale, key=lambda t: t["dir"]):
            if fix(args.job, trial, dry_run=args.dry_run):
                replaced += 1
            else:
                failed += 1
    else:
        for harness in harnesses:
            for model in models:
                job = latest_job(jobs, harness, model, tasks)
                if job is None:
                    print(f"{harness} {model}: no job covers {','.join(tasks)}")
                    continue
                stale = [t for t in job["trials"] if t["exception"] or t["over_budget"]]
                print(f"{harness} {model}: {job['name']} has {len(stale)} stale trial(s)")
                for trial in sorted(stale, key=lambda t: t["dir"]):
                    if fix(job["name"], trial, dry_run=args.dry_run):
                        replaced += 1
                    else:
                        failed += 1

    print(f"replaced {replaced} trial(s), {failed} failure(s)" if not args.dry_run else "dry run only")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
