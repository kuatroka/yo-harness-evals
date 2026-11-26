#!/usr/bin/env python3
"""Redo the alacritty trials that the removed ten-minute agent window stopped.

The bench tasks used to declare a 600 second agent budget. These five jobs each
carry one or two alacritty trials that hit it, so their scores describe the clock
rather than the model. Each rerun is a fresh one-task job under the current
policy, which has no agent limit; `replace_trial.py` then moves the rerun into
the job it corrects and archives the trial it displaces.

The floor task is left alone. `ripgrep-2555` fails every pair that finishes
inside the old budget, so its capped trials are not believed to be hiding a pass.

Usage:
    python3 scripts/uncap_alacritty.py --dry-run
    python3 scripts/uncap_alacritty.py
"""

from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK = "alacritty__alacritty-3921"

TARGETS = (
    {
        "tag": "ampflash",
        "harness": "amp",
        "model": "swe-2-flash-max",
        "attempts": 1,
        "job": "fh-amp-swe-2-flash-max-b-111839",
        "old": ("alacritty__alacritty-3921__sEBAFUq",),
    },
    {
        "tag": "claudeflash",
        "harness": "claude",
        "model": "swe-2-flash-max",
        "attempts": 1,
        "job": "fh-claude-swe-2-flash-max-b-111839",
        "old": ("alacritty__alacritty-3921__4V3pDz7",),
    },
    {
        "tag": "droidflash",
        "harness": "droid",
        "model": "swe-2-flash-max",
        "attempts": 2,
        "job": "fh-droid-swe-2-flash-max-b-111839",
        "old": ("alacritty__alacritty-3921__ELJpSkr", "alacritty__alacritty-3921__EqkwHAw"),
    },
    {
        "tag": "droidhaiku",
        "harness": "droid",
        "model": "haiku-devin-max",
        "attempts": 2,
        "job": "fh-droid-haiku-devin-max-b-125023",
        "old": ("alacritty__alacritty-3921__YPKwYkA", "alacritty__alacritty-3921__bPBhq5e"),
    },
    {
        "tag": "claudehaiku",
        "harness": "claude",
        "model": "haiku-anthropic-direct",
        "attempts": 1,
        "job": "fh-claude-haiku-anthropic-direct-maxhaiku-131833",
        "old": ("alacritty__alacritty-3921__XwdjJEA",),
    },
)


def newest_job(prefix: str) -> Path | None:
    matches = sorted(
        (p for p in (ROOT / "jobs").glob(prefix) if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    return matches[-1] if matches else None


def rerun(target: dict, *, dry_run: bool) -> Path | None:
    tag = f"uncapped-{target['tag']}"
    run_id = f"{tag}-{datetime.datetime.now():%H%M%S}"
    env = dict(os.environ)
    env.update(
        DATASET=str(ROOT / "datasets" / "bench-fast"),
        TASKS=TASK,
        HARNESSES=target["harness"],
        MODELS=target["model"],
        ATTEMPTS=str(target["attempts"]),
        RETRIES="2",
        CONCURRENCY="2",
        RUN_ID=run_id,
    )
    if dry_run:
        print(f"would run {target['harness']} {target['model']} x{target['attempts']} as {run_id}")
        return None
    print(f"running {target['harness']} {target['model']} x{target['attempts']} as {run_id}", flush=True)
    subprocess.run(["bash", str(ROOT / "scripts" / "run_matrix.sh")], env=env, cwd=ROOT, check=False)
    return newest_job(f"fh-*{run_id}*")


def replace(original: str, old: tuple[str, ...], job: Path, *, dry_run: bool) -> int:
    fresh = sorted(p.name for p in job.iterdir() if (p / "result.json").is_file())
    if len(fresh) < len(old):
        print(f"  {job.name}: {len(fresh)} trial(s), expected {len(old)}; nothing replaced")
        return 0
    replaced = 0
    for old_name, new_name in zip(old, fresh):
        command = [
            sys.executable,
            str(ROOT / "scripts" / "replace_trial.py"),
            "--job",
            str(ROOT / "jobs" / original),
            "--old",
            old_name,
            "--new",
            str(job / new_name),
        ]
        if dry_run:
            print("  would run:", " ".join(command[1:]))
            continue
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        print(f"  {old_name} -> {new_name}: exit {result.returncode} {result.stdout.strip()[:120]}")
        replaced += 1 if result.returncode == 0 else 0
    return replaced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    total = 0
    for target in TARGETS:
        job = rerun(target, dry_run=args.dry_run)
        if job is None:
            continue
        total += replace(target["job"], target["old"], job, dry_run=args.dry_run)
    print(f"replaced {total} trial(s)" if not args.dry_run else "dry run only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
