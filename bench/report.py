"""Report the harness comparison for a model, with the decision rule applied.

Two tables come out. The first summarises every harness and model cell. The
second compares two harnesses on the tasks they both ran, and returns a verdict
only when the evidence clears a rule that was written down before the run. A gap
smaller than that is reported as no measurable difference, and the tie is broken
on measured latency, which has more statistical power than a binary reward.

    .venv/bin/python bench/report.py --pair droid,amp
    .venv/bin/python bench/report.py --pair droid,amp --tier fast --models swe-2-max
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

# Running as a script puts bench/ on sys.path, the package import path does not.
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import list_cost, load_config, load_trials, resolve_model, summarise  # noqa: E402

DEFAULT_JOBS_DIR = Path(__file__).resolve().parent.parent / "jobs"
DEFAULT_MANIFEST = Path(__file__).resolve().parent / "config" / "tasks.json"


def verdict(per_task: dict[str, dict[str, float]], pair: tuple[str, str], rule: dict):
    """Apply the pre-registered rule to matched per-task means."""
    left, right = pair
    shared = sorted(set(per_task[left]) & set(per_task[right]))
    if not shared:
        return None
    left_mean = sum(per_task[left][task] for task in shared) / len(shared)
    right_mean = sum(per_task[right][task] for task in shared) / len(shared)
    left_wins = sum(1 for task in shared if per_task[left][task] > per_task[right][task])
    right_wins = sum(1 for task in shared if per_task[right][task] > per_task[left][task])
    gap = left_mean - right_mean
    if len(shared) < rule.get("min_tasks", 3):
        return (
            "insufficient",
            f"only {len(shared)} shared task(s), need {rule.get('min_tasks', 3)}",
            gap,
            shared,
            left_wins,
            right_wins,
        )

    leader = left if gap > 0 else right
    wins = max(left_wins, right_wins)
    if wins >= rule["min_discordant"]:
        return leader, f"{wins} of {len(shared)} shared tasks favour {leader}", gap, shared, left_wins, right_wins
    if abs(gap) >= rule["min_gap"]:
        return leader, f"matched gap {abs(gap):.3f} clears {rule['min_gap']}", gap, shared, left_wins, right_wins
    return "tie", f"no measurable difference (gap {abs(gap):.3f}, {wins} discordant of {len(shared)})", gap, shared, left_wins, right_wins


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS_DIR)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--pair", default="droid,amp", help="two harnesses, left,right")
    parser.add_argument("--tier", default="all", choices=["all", "fast", "slow", "unmeasured"])
    parser.add_argument("--models", default="all", help="comma separated model keys, or all")
    parser.add_argument("--harnesses", default="", help="optional harness filter for the summary table")
    parser.add_argument(
        "--include-saturated",
        action="store_true",
        help="let tasks that every model saturates into the verdict, which dilutes it",
    )
    args = parser.parse_args(argv)

    models = load_config("models")
    trials = [trial for trial in load_trials(args.jobs_dir) if trial.finished]

    manifest = json.loads(args.manifest.read_text()) if args.manifest.is_file() else {"tasks": {}, "gate": {}}
    rule = {
        "min_discordant": manifest["gate"].get("min_discordant", 6),
        "min_gap": manifest["gate"].get("min_gap", 0.10),
        "min_tasks": manifest["gate"].get("min_tasks", 3),
    }
    if args.tier != "all":
        allowed = {name for name, entry in manifest["tasks"].items() if entry.get("tier") == args.tier}
        trials = [trial for trial in trials if trial.task in allowed]
    if not args.include_saturated:
        live = {name for name, entry in manifest["tasks"].items() if entry.get("discriminates")}
        if live:
            trials = [trial for trial in trials if trial.task in live]
    if args.harnesses:
        wanted = {name.strip() for name in args.harnesses.split(",")}
        trials = [trial for trial in trials if trial.harness in wanted]

    cells: dict[tuple, list] = defaultdict(list)
    for trial in trials:
        cells[(resolve_model(trial.model, models), trial.harness, trial.task)].append(trial)

    print(f"{'model':22s} {'harness':8s} {'trials':>6s} {'to':>3s} {'mean':>6s} {'mean*':>6s} {'p50':>6s} {'p90':>6s} {'usd/tr':>7s} {'Input':>12s} {'Cashed':>12s} {'Output':>11s} {'Total':>13s}")
    for key in sorted({cell[0] or "unknown" for cell in cells}):
        if args.models != "all" and key not in {name.strip() for name in args.models.split(",")}:
            continue
        for harness in sorted({cell[1] or "unknown" for cell in cells if cell[0] == key}):
            grouped = [trial for (k, h, _), batch in cells.items() if k == key and h == harness for trial in batch]
            stats = summarise(grouped)
            trials_n = stats.n_usable + stats.n_timeout
            costs = [cost for cost in (list_cost(trial, models) for trial in grouped) if cost is not None]
            fmt = lambda value: "-" if value is None else f"{value:.2f}"  # noqa: E731
            fmti = lambda value: "-" if value is None else f"{value:,}"  # noqa: E731
            # Per trial, not per cell: a harness whose token counts are missing
            # would otherwise look cheap simply by having fewer priced trials.
            usd = f"{sum(costs) / len(costs):.2f}" if costs else "-"
            inputs = sum(t.tokens_in or 0 for t in grouped)
            cached = sum(t.tokens_cached or 0 for t in grouped)
            outputs = sum(t.tokens_out or 0 for t in grouped)
            total = inputs + outputs
            print(
                f"{key:22s} {harness:8s} {trials_n:6d} {stats.n_timeout:3d} "
                f"{fmt(stats.mean_without_timeouts):>6s} {fmt(stats.mean_with_timeouts):>6s} "
                f"{fmt(stats.p50_minutes):>6s} {fmt(stats.p90_minutes):>6s} {usd:>7s} "
                f"{fmti(inputs):>12s} {fmti(cached):>12s} {fmti(outputs):>11s} {fmti(total):>13s}"
            )

    left, right = [name.strip() for name in args.pair.split(",")]
    print(f"\npaired {left} vs {right}")
    for key in sorted({cell[0] or "unknown" for cell in cells}):
        per_task: dict[str, dict[str, float]] = {left: {}, right: {}}
        for (model_key, harness, task), batch in cells.items():
            if model_key != key or harness not in per_task:
                continue
            stats = summarise(batch)
            if stats.mean_with_timeouts is not None:
                per_task[harness][task] = stats.mean_with_timeouts
        result = verdict(per_task, (left, right), rule)
        if result is None:
            continue
        winner, reason, gap, shared, left_wins, right_wins = result
        print(f"  {key:22s} tasks={len(shared):2d} {left}={sum(per_task[left][t] for t in shared)/len(shared):.3f} "
              f"{right}={sum(per_task[right][t] for t in shared)/len(shared):.3f} "
              f"ledger={left_wins}-{right_wins} -> {winner} ({reason})")
    print("\nmean* counts a timeout as 0.0. mean leaves it out. usd/tr is the registry list price per priced trial, not a bill.")
    if not args.include_saturated:
        print("Saturated tasks are excluded from the verdict. Pass --include-saturated to see how they dilute it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
