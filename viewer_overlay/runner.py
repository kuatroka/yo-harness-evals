"""Starting a run and reading back what happened to it.

The launch record owns launch state, `jobs/` owns job state, and the registry
owns routing. Nothing here caches: status is a glob and a few file reads on
every request, so a run that finishes between two page loads shows up, and a
server restart changes nothing.

`scripts/run_matrix.sh` stays the only launcher. This module builds its
environment, records why, and starts it in its own session so the viewer can
die without taking a nine-hour matrix down with it.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .launch import LaunchSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_MATRIX = Path("scripts/run_matrix.sh")
COMMAND = ["bash", str(RUN_MATRIX)]
LOGS_DIR = "logs"
JOBS_DIR = "jobs"
LAUNCHES_DIR = "launches"


@dataclass(frozen=True)
class Launch:
    launch_id: str
    job_prefix: str
    log_path: Path
    record_path: Path
    spawned: bool


def start(
    spec: LaunchSpec,
    repo_root: Path = REPO_ROOT,
    dry_run: bool | None = None,
) -> Launch:
    """Record the launch, then start the matrix unless this is a dry run."""
    if dry_run is None:
        dry_run = os.environ.get("OVERLAY_DRY_RUN") == "1"

    now = datetime.now(timezone.utc)
    launch_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    job_prefix = f"ui-{launch_id}"
    run_id = now.strftime("%Y%m%d-%H%M%S")

    logs_dir = repo_root / LOGS_DIR
    log_path = logs_dir / f"{job_prefix}.log"
    record_path = logs_dir / LAUNCHES_DIR / f"{launch_id}.json"
    env = build_env(spec, launch_id=launch_id, run_id=run_id, repo_root=repo_root)
    record: dict[str, Any] = {
        "launch_id": launch_id,
        "created_at": _utc_text(now),
        "spec": spec.as_dict(),
        "job_prefix": job_prefix,
        "run_id": run_id,
        "log_path": str(log_path),
        "command": COMMAND,
        "dry_run": dry_run,
        "env": env,
    }

    logs_dir.mkdir(parents=True, exist_ok=True)
    if not write_record(record_path, record):
        # The same id already has a record, so this launch already happened.
        return Launch(launch_id, job_prefix, log_path, record_path, spawned=False)

    spawned = False
    if dry_run:
        _write_dry_run_log(log_path, record)
    else:
        with log_path.open("a", encoding="utf-8") as log:
            subprocess.Popen(  # noqa: S603 - fixed command, fixed cwd
                COMMAND,
                cwd=str(repo_root),
                env={**os.environ, **env},
                start_new_session=True,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        spawned = True
    return Launch(launch_id, job_prefix, log_path, record_path, spawned=spawned)


def build_env(
    spec: LaunchSpec,
    launch_id: str,
    run_id: str,
    repo_root: Path = REPO_ROOT,
) -> dict[str, str]:
    """The matrix's own variables, derived from the spec and nothing else."""
    return {
        "DATASET": str(repo_root / "datasets" / spec.dataset),
        "JOBS_DIR": str(repo_root / JOBS_DIR),
        "LOG_DIR": str(repo_root / LOGS_DIR),
        "ATTEMPTS": str(spec.attempts),
        "RETRIES": str(spec.retries),
        "CONCURRENCY": str(spec.n_concurrent),
        "RUN_ID": run_id,
        "JOB_PREFIX": f"ui-{launch_id}",
        "HARNESSES": spec.agent,
        "MODELS": spec.model,
        "EFFORTS": spec.effort,
        "TASKS": ",".join(spec.tasks),
    }


def write_record(path: Path, record: dict[str, Any]) -> bool:
    """Write a launch record once. A second write for the same id is a no-op.

    The bytes land through a temporary file, so a crash leaves no half-written
    record that later reads would have to guess about.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return True


def list_launches(repo_root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    """Every launch record, with its jobs' state read fresh from disk."""
    logs_dir = repo_root / LOGS_DIR
    jobs_dir = repo_root / JOBS_DIR
    launches: list[dict[str, Any]] = []
    for path in sorted((logs_dir / LAUNCHES_DIR).glob("*.json")):
        record = _read_json(path)
        if not isinstance(record, dict):
            continue
        job_prefix = record.get("job_prefix")
        job_dirs = (
            sorted(jobs_dir.glob(f"{job_prefix}-*"))
            if isinstance(job_prefix, str) and job_prefix
            else []
        )
        jobs = [
            {"job_name": job_dir.name, "state": job_state(job_dir)}
            for job_dir in job_dirs
            if job_dir.is_dir()
        ]
        launches.append(
            {
                "launch_id": record.get("launch_id", path.stem),
                "created_at": record.get("created_at"),
                "spec": record.get("spec"),
                "status": launch_status([job["state"] for job in jobs]),
                "jobs": jobs,
            }
        )
    launches.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return launches


def job_state(job_dir: Path) -> str:
    """Read one job directory. Harbor writes result.json at start, then fills it."""
    result = _read_json(job_dir / "result.json")
    if not isinstance(result, dict) or not result.get("finished_at"):
        return "running"
    stats = result.get("stats")
    errored = stats.get("n_errored_trials") if isinstance(stats, dict) else 0
    return "failed" if errored else "completed"


def launch_status(states: list[str]) -> str:
    if not states:
        return "pending"
    if any(state == "running" for state in states):
        return "running"
    if any(state == "failed" for state in states):
        return "failed"
    return "completed"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _utc_text(now: datetime) -> str:
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_dry_run_log(path: Path, record: dict[str, Any]) -> None:
    lines = [
        "# overlay dry run: nothing spawned",
        f"command: {' '.join(record['command'])}",
        "env:",
    ]
    lines.extend(f"{key}={record['env'][key]}" for key in sorted(record["env"]))
    with path.open("a", encoding="utf-8") as log:
        log.write("\n".join(lines) + "\n")
