"""Per-task dump for the two signal tasks, grouped by model id and harness."""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))

import lib  # noqa: E402

TASKS = {"alacritty__alacritty-3921", "burntsushi__ripgrep-2555"}


def main() -> int:
    trials = [t for t in lib.load_trials(ROOT / "jobs") if t.task in TASKS]
    for task in sorted(TASKS):
        print(f"\n{task}")
        rows = defaultdict(list)
        for t in trials:
            if t.task != task:
                continue
            rows[(t.model, t.harness)].append(t)
        for (model, harness), group in sorted(rows.items()):
            rewards = [t.reward for t in group]
            timeouts = sum(1 for t in group if t.timed_out)
            mean = sum(r for r in rewards if r is not None) / len(rewards) if rewards else 0.0
            minutes = ", ".join(f"{t.duration_min:.1f}" for t in group)
            print(f"  {model:<24} {harness:<7} n={len(group)} to={timeouts} mean={mean:.2f}  {minutes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
