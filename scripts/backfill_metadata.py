#!/usr/bin/env python3
"""Fill in the reasoning effort the viewer shows for trials that lack it.

Harbor's viewer reads effort from config.agent.kwargs["reasoning_effort"].
Droid and Amp runs pass it as a kwarg. Claude Code runs on a Devin route encode
effort in the model ID (devin/swe-2-max, devin/swe-2-flash-max), so those trials
arrive with empty kwargs and blank the Effort column. A direct Anthropic route
carries no suffix and gets the kwarg instead, so it needs no backfill.

The suffix of the model ID is the effort that actually ran, so we record it.
Only that one key is written, and only when it is missing. Every other value in
the trial is left byte for byte as Harbor wrote it.

Usage:
    python3 scripts/backfill_metadata.py --root jobs --dry-run
    python3 scripts/backfill_metadata.py --root jobs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EFFORT_SUFFIXES = ("max", "high", "medium", "low", "minimal")


def effort_from_model(model_name: str | None) -> str | None:
    """Return the effort encoded in a model ID, or None when it carries none.

    Two spellings reach the viewer. Droid and Amp routes end in ``-max`` or
    ``-high``; 9Router's Engy catalogue spells it ``(max)`` at the end.
    """
    if not model_name:
        return None
    tail = model_name.rsplit("/", 1)[-1]
    if tail.endswith(")") and "(" in tail:
        inner = tail.rsplit("(", 1)[1][:-1]
        if inner in EFFORT_SUFFIXES:
            return inner
        return None
    for suffix in EFFORT_SUFFIXES:
        if tail.endswith(f"-{suffix}"):
            return suffix
    return None


def patch_config(config: dict[str, Any]) -> str | None:
    """Set the effort on a trial config in place. Return it when changed."""
    agent = config.get("agent")
    if not isinstance(agent, dict):
        return None
    kwargs = agent.setdefault("kwargs", {})
    if not isinstance(kwargs, dict):
        return None
    if kwargs.get("reasoning_effort"):
        return None
    effort = effort_from_model(agent.get("model_name"))
    if effort is None:
        return None
    kwargs["reasoning_effort"] = effort
    return effort


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Match Harbor's on-disk format: four-space indent, no trailing newline."""
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")


def backfill_trial(trial_dir: Path, *, dry_run: bool) -> str | None:
    result_path = trial_dir / "result.json"
    if not result_path.is_file():
        return None
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"skip unreadable {result_path}", file=sys.stderr)
        return None
    config = result.get("config")
    if not isinstance(config, dict):
        return None
    effort = patch_config(config)
    if effort is None:
        return None
    if dry_run:
        return effort
    write_json(result_path, result)
    write_json(trial_dir / "config.json", config)
    return effort


def iter_trials(root: Path) -> list[Path]:
    """Return trial directories under root, which may be a jobs dir or one job."""
    if (root / "result.json").is_file():
        return [p for p in sorted(root.iterdir()) if (p / "result.json").is_file()]
    trials: list[Path] = []
    for job in sorted(root.iterdir()):
        if not (job / "result.json").is_file():
            continue
        trials.extend(p for p in sorted(job.iterdir()) if (p / "result.json").is_file())
    return trials


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="jobs", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.root.is_dir():
        print(f"no such directory: {args.root}", file=sys.stderr)
        return 2

    scanned = filled = 0
    for trial_dir in iter_trials(args.root):
        scanned += 1
        effort = backfill_trial(trial_dir, dry_run=args.dry_run)
        if effort is not None:
            filled += 1
            print(f"{trial_dir.name}: reasoning_effort={effort}")

    verb = "would fill" if args.dry_run else "filled"
    print(f"scanned {scanned} trials, {verb} {filled}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
