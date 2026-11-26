"""Measure each task and write the generated task manifest.

A task earns a place in a tier by measurement, never by reputation. The gate is
a p90 wall clock under the configured ceiling, taken as the worst case over every
model base that has run the task, because runtime is a property of the task and
model pair. A max-thinking model can spend longer on a reasoning problem than a
smaller one, so the slowest base bounds the task's cost.

The ceiling is a preference about turnaround, not a claim about capability. Set it
where a full batch still finishes inside the time you are willing to wait, and
read p50 when choosing between tasks that both qualify.

Run it after any batch that adds tasks or models:

    .venv/bin/python bench/calibrate.py
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Running as a script puts bench/ on sys.path, the package import path does not.
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import base_of, load_config, load_trials, percentile, trial_model_key  # noqa: E402

DEFAULT_GATE_MINUTES = 20.0
DEFAULT_JOBS_DIR = Path(__file__).resolve().parent.parent / "jobs"


def _source_label(path: Path) -> str:
    """A repo-relative label, so a tracked manifest carries no host path."""
    root = Path(__file__).resolve().parent.parent
    try:
        return str(Path(path).resolve().relative_to(root))
    except ValueError:
        return str(path)


def measure(trials, models):
    """Group finished trials into per-task, per-base measurements."""
    durations: dict[tuple, list[float]] = defaultdict(list)
    rewards: dict[tuple, list[float]] = defaultdict(list)
    honest: dict[tuple, list[float]] = defaultdict(list)
    for trial in trials:
        base = base_of(trial_model_key(trial, models), models) or (trial.model or "unknown")
        # A timed-out trial ran until the budget stopped it. Its duration measures
        # the clock, not the task, so it would inflate the percentile that decides
        # whether the task is cheap enough to keep.
        if trial.duration_min is not None and not trial.timed_out:
            durations[(trial.task, base)].append(trial.duration_min)
        if trial.reward is not None:
            rewards[(trial.task, base)].append(trial.reward)
            honest[(trial.task, base)].append(trial.reward)
        elif trial.timed_out:
            honest[(trial.task, base)].append(0.0)

    tasks: dict[str, dict] = {}
    for task, base in durations.keys() | rewards.keys() | honest.keys():
        entry = tasks.setdefault(
            task, {"p50": {}, "p90": {}, "band": {}, "band_honest": {}, "n": {}}
        )
        if (task, base) in durations:
            values = durations[(task, base)]
            entry["p50"][base] = round(percentile(values, 0.5), 1)
            entry["p90"][base] = round(percentile(values, 0.9), 1)
        if (task, base) in rewards:
            entry["n"][base] = len(rewards[(task, base)])
            entry["band"][base] = round(sum(rewards[(task, base)]) / len(rewards[(task, base)]), 3)
        if (task, base) in honest:
            entry["band_honest"][base] = round(sum(honest[(task, base)]) / len(honest[(task, base)]), 3)
    return tasks


def classify(tasks: dict, gate_minutes: float) -> None:
    for entry in tasks.values():
        worst = max(entry["p90"].values()) if entry["p90"] else None
        entry["worst_p90"] = worst
        if worst is None:
            entry["tier"] = "unmeasured"
        elif worst <= gate_minutes:
            entry["tier"] = "fast"
        else:
            entry["tier"] = "slow"
        entry["discriminates"] = any(0.0 < value < 1.0 for value in entry.get("band", {}).values())


def render(tasks: dict) -> str:
    header = f"{'task':30s} {'tier':10s} {'worst p90':>9s} {'disc':>5s}  bands"
    lines = [header]
    for task in sorted(tasks, key=lambda name: (tasks[name]["tier"], name)):
        entry = tasks[task]
        bands = " ".join(f"{base}={value:.2f}" for base, value in sorted(entry.get("band", {}).items()))
        worst = "-" if entry["worst_p90"] is None else f"{entry['worst_p90']:.1f}"
        lines.append(f"{task:30s} {entry['tier']:10s} {worst:>9s} {str(entry['discriminates']):>5s}  {bands}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS_DIR)
    parser.add_argument("--gate-minutes", type=float, default=DEFAULT_GATE_MINUTES)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "config" / "tasks.json")
    args = parser.parse_args(argv)

    models = load_config("models")
    tasks = measure(load_trials(args.jobs_dir), models)
    classify(tasks, args.gate_minutes)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": _source_label(args.jobs_dir),
        "gate": {
            "p90_minutes": args.gate_minutes,
            "band": [0.1, 0.9],
            "min_discordant": 6,
            "min_gap": 0.10,
            "min_tasks": 3,
        },
        "tasks": tasks,
    }
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(render(tasks))
    fast = [name for name, entry in tasks.items() if entry["tier"] == "fast"]
    live = [name for name in fast if tasks[name]["discriminates"]]
    print(f"\nfast: {len(fast)} | fast and discriminating: {len(live)} {sorted(live)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
