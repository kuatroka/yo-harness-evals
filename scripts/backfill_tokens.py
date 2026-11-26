#!/usr/bin/env python3
"""Fill in token totals for finished trials that recorded none.

Harbor leaves ``agent_result``'s token counts null when it cannot read usage
from the agent's own logs. Every finished trial with no exception is fair game
here, because a null reads as "unknown" in the viewer while the run did happen.

The usage source is the agent's own log, never a guess:

* ``amp.txt`` holds per-turn usage in stream-json assistant events.
* ``droid.txt`` holds one final ``result`` event with usage.
* ``codex.txt`` holds one ``turn.completed`` event with usage. An all-zero
  usage is written as zeros: the turn completed and consumed nothing, which is
  true of a run blocked upstream.
* Claude Code needs its session converted, because Harbor drops the whole
  trajectory when a nested CLI left a second session directory behind. The
  harness fix in ``harnesses/claude.py`` selects the launched session, and this
  script runs that same conversion.

Old runs whose logs kept only the final agent text (some Amp and Droid trials
from 18 November) have no usage anywhere and stay null. The script reports
them instead of inventing a number.

Only ``n_input_tokens``, ``n_output_tokens`` and ``n_cache_tokens`` are
written. Everything else in the trial stays byte for byte as Harbor wrote it.

Usage:
    uv run python scripts/backfill_tokens.py --root jobs --dry-run
    uv run python scripts/backfill_tokens.py --root jobs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from harnesses.claude import ClaudeCodeAgent
from harnesses.common import amp_usage_totals, droid_usage_totals

Usage = tuple[int, int, int]


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Match Harbor's on-disk format: four-space indent, no trailing newline."""
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")


def codex_usage(stream: Path) -> Usage | None:
    """Read the usage Codex reports when a turn completes.

    The last event wins: a turn's usage is cumulative for that turn, and the
    file can hold several turns.
    """
    usage: dict[str, Any] | None = None
    try:
        lines = stream.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidate = event.get("usage")
        if event.get("type") == "turn.completed" and isinstance(candidate, dict):
            usage = candidate
    if usage is None:
        return None
    return (
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        int(usage.get("cached_input_tokens") or 0),
    )


def claude_usage(trial_dir: Path) -> Usage | None:
    """Convert the launched Claude session and read its totals."""
    logs = trial_dir / "agent"
    model_name: str | None = None
    skip_model_parsing = False
    try:
        config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        config = {}
    agent_config = config.get("agent")
    if isinstance(agent_config, dict):
        model_name = agent_config.get("model_name")
        skip_model_parsing = bool(agent_config.get("skip_model_parsing"))
    agent = ClaudeCodeAgent(
        logs, model_name=model_name, skip_model_parsing=skip_model_parsing
    )
    trajectory = agent.convert_trajectory(logs)
    metrics = trajectory.final_metrics if trajectory is not None else None
    if metrics is None or metrics.total_prompt_tokens is None:
        return None
    return (
        metrics.total_prompt_tokens,
        metrics.total_completion_tokens or 0,
        metrics.total_cached_tokens or 0,
    )


def usage_from_trial(trial_dir: Path) -> Usage | None:
    """Return the token counts recoverable from this trial's agent logs."""
    agent = trial_dir / "agent"
    if (agent / "amp.txt").is_file():
        return amp_usage_totals(agent / "amp.txt")
    if (agent / "droid.txt").is_file():
        return droid_usage_totals(agent / "droid.txt")
    if (agent / "codex.txt").is_file():
        return codex_usage(agent / "codex.txt")
    if (agent / "claude-code.txt").is_file():
        return claude_usage(trial_dir)
    return None


def is_eligible(result: dict[str, Any]) -> bool:
    """A trial worth filling: finished, scored, no exception, no token data."""
    if result.get("exception_info") is not None:
        return False
    if not result.get("finished_at"):
        return False
    verifier = result.get("verifier_result")
    if not isinstance(verifier, dict) or not verifier.get("rewards"):
        return False
    agent_result = result.get("agent_result")
    if not isinstance(agent_result, dict):
        return False
    return (
        agent_result.get("n_input_tokens") is None
        and agent_result.get("n_output_tokens") is None
    )


def backfill_trial(trial_dir: Path, *, dry_run: bool) -> Usage | None:
    """Write the recovered usage into the trial result. Return it when found."""
    result_path = trial_dir / "result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not is_eligible(result):
        return None
    usage = usage_from_trial(trial_dir)
    if usage is None:
        return None
    agent_result = result["agent_result"]
    agent_result["n_input_tokens"], agent_result["n_output_tokens"] = (
        usage[0],
        usage[1],
    )
    agent_result["n_cache_tokens"] = usage[2]
    if not dry_run:
        write_json(result_path, result)
    return usage


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

    scanned = filled = unrecoverable = 0
    for trial_dir in iter_trials(args.root):
        scanned += 1
        try:
            result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not is_eligible(result):
            continue
        usage = backfill_trial(trial_dir, dry_run=args.dry_run)
        if usage is None:
            unrecoverable += 1
            print(f"no usage in logs: {trial_dir}")
            continue
        filled += 1
        label = "zero" if usage == (0, 0, 0) else "usage"
        print(
            f"{label}: {trial_dir} "
            f"input={usage[0]} output={usage[1]} cache={usage[2]}"
        )

    verb = "would fill" if args.dry_run else "filled"
    print(
        f"scanned {scanned} trials, {verb} {filled}, "
        f"{unrecoverable} clean trials with no usage in their logs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
