#!/usr/bin/env python3
"""Replace one finished trial in a job with a rerun of the same task.

The rerun lands as a separate job, which keeps every attempt auditable. This
script moves the replacement into the original job so the job mean reflects the
new attempt instead of carrying a stale one, and archives the trial it replaced
outside the jobs tree so nothing is lost.

Only the trial directory, its trial name, and the job's aggregate stats change.
Task checksums, verifier output and the agent record come from the replacement
exactly as Harbor wrote them.

Usage:
    python3 scripts/replace_trial.py --job jobs/<job> --old <trial_name> \
        --new jobs/<rerun_job>/<trial_name> --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ARCHIVE_ROOT = Path(__file__).resolve().parent.parent / "archive" / "replaced-trials"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")


def trial_dirs(job: Path) -> list[Path]:
    return [p for p in sorted(job.iterdir()) if (p / "result.json").is_file()]


def reward_of(result: dict[str, Any]) -> float | None:
    rewards = ((result.get("verifier_result") or {}).get("rewards")) or {}
    value = rewards.get("reward")
    return float(value) if value is not None else None


def exception_of(result: dict[str, Any]) -> str | None:
    return (result.get("exception_info") or {}).get("exception_type")


def pass_at_k(rewards_by_task: dict[str, list[float | None]]) -> dict[str, float]:
    """Share of tasks with at least one success across their attempts."""
    if not rewards_by_task:
        return {}
    k = max(len(r) for r in rewards_by_task.values())
    solved = sum(1 for rs in rewards_by_task.values() if any(r == 1.0 for r in rs))
    return {str(k): solved / len(rewards_by_task)}


def recompute_stats(job: Path) -> dict[str, Any]:
    """Rebuild the aggregate block of a single-eval job from its trials."""
    job_result = load_json(job / "result.json")
    stats = job_result.setdefault("stats", {})
    evals = stats.get("evals") or {}
    if len(evals) != 1:
        raise SystemExit(f"{job.name}: expected exactly one eval entry, found {len(evals)}")
    eval_key = next(iter(evals))

    rewards: list[float] = []
    reward_stats: dict[str, list[str]] = {}
    exception_stats: dict[str, list[str]] = {}
    rewards_by_task: dict[str, list[float | None]] = {}
    tokens = {"input": 0, "cache": 0, "output": 0}
    errored = 0

    for trial in trial_dirs(job):
        result = load_json(trial / "result.json")
        name = result.get("trial_name") or trial.name
        task = (result.get("task_name") or "").split("/")[-1]
        reward = reward_of(result)
        exc = exception_of(result)
        if exc:
            errored += 1
            exception_stats.setdefault(exc, []).append(name)
        rewards_by_task.setdefault(task, []).append(reward)
        if reward is not None and result.get("reward_valid", True):
            rewards.append(reward)
            reward_stats.setdefault(str(reward), []).append(name)
        agent = result.get("agent_result") or {}
        tokens["input"] += agent.get("n_input_tokens") or 0
        tokens["cache"] += agent.get("n_cache_tokens") or 0
        tokens["output"] += agent.get("n_output_tokens") or 0

    n_trials = len(trial_dirs(job))
    stats.update(
        {
            "n_completed_trials": n_trials,
            "n_errored_trials": errored,
            "n_running_trials": 0,
            "n_pending_trials": 0,
            "n_cancelled_trials": 0,
            "n_input_tokens": tokens["input"],
            "n_cache_tokens": tokens["cache"],
            "n_output_tokens": tokens["output"],
        }
    )
    evals[eval_key] = {
        "n_trials": n_trials,
        "n_errors": errored,
        "metrics": [{"mean": sum(rewards) / len(rewards) if rewards else 0.0}],
        "pass_at_k": pass_at_k(rewards_by_task),
        "reward_stats": {"reward": reward_stats},
        "exception_stats": exception_stats,
    }
    job_result["updated_at"] = datetime.now(timezone.utc).isoformat()
    return job_result


def replace(job: Path, old_name: str, new_dir: Path, *, dry_run: bool) -> dict[str, Any]:
    old_dir = job / old_name
    if not (old_dir / "result.json").is_file():
        raise SystemExit(f"no such trial in job: {old_dir}")
    if not (new_dir / "result.json").is_file():
        raise SystemExit(f"no result to take from: {new_dir}")
    new_result = load_json(new_dir / "result.json")
    if not new_result.get("finished_at"):
        raise SystemExit(f"replacement has not finished: {new_dir}")

    old_result = load_json(old_dir / "result.json")
    if (old_result.get("task_name") or "") != (new_result.get("task_name") or ""):
        raise SystemExit(
            f"task mismatch: {old_result.get('task_name')} vs {new_result.get('task_name')}"
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archive = ARCHIVE_ROOT / job.name / f"{old_name}-{stamp}"
    summary = {
        "job": job.name,
        "old_trial": old_name,
        "new_trial": new_dir.name,
        "task": old_result.get("task_name"),
        "old_exception": exception_of(old_result),
        "old_reward": reward_of(old_result),
        "new_exception": exception_of(new_result),
        "new_reward": reward_of(new_result),
        "archive": str(archive),
    }
    if dry_run:
        return summary

    archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(old_dir, archive)
    shutil.rmtree(old_dir)
    shutil.copytree(new_dir, old_dir)

    for name in ("result.json", "config.json"):
        path = old_dir / name
        if not path.is_file():
            continue
        data = load_json(path)
        data["trial_name"] = old_name
        if name == "result.json":
            data["trial_uri"] = old_result.get("trial_uri")
            data["replaced_from"] = new_dir.name
            data["replaced_at"] = stamp
        write_json(path, data)

    write_json(archive.parent / f"replacement-{stamp}-{old_name}.json", summary)
    write_json(job / "result.json", recompute_stats(job))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    summary = replace(args.job, args.old, args.new, dry_run=args.dry_run)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
