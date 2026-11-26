"""Plan, or run, a comparison from the registries.

The planner is the only place that knows how a selection becomes a Harbor job.
It refuses combinations the registries mark unsupported, so a route that does not
exist fails here instead of halfway through a batch, and it never asks for more
concurrency than the narrowest selected harness allows.

    .venv/bin/python bench/plan.py --tier fast --discriminating --models 9r/dv-bridge/swe-2-max --attempts 3
    .venv/bin/python bench/plan.py --tier fast --models 9r/dv-bridge/swe-2-max --attempts 3 --execute
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

# Running as a script puts bench/ on sys.path, the package import path does not.
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "scripts" / "run_matrix.sh"


def concurrency_cap(
    harnesses: list[str],
    models: list[str],
    registry: dict,
    caps: dict,
) -> int:
    """Narrowest limit across the selected harnesses and model rows.

    Devin's guide caps swe-2 at two concurrent calls, so the row carries its
    own cap. A wide harness cannot widen a narrow model.
    """
    limits = [caps[harness]["max_concurrency"] for harness in harnesses]
    limits += [
        registry[model]["max_concurrency"]
        for model in models
        if registry[model].get("max_concurrency")
    ]
    return min(limits)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harnesses", default="droid,amp")
    parser.add_argument("--models", required=True, help="comma separated ids from models.json")
    parser.add_argument("--tier", default="fast", choices=["fast", "slow", "unmeasured", "all"])
    parser.add_argument("--discriminating", action="store_true", help="keep only tasks with a live band")
    parser.add_argument("--tasks", default="", help="override the task list")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--tag", default="bench")
    parser.add_argument("--manifest", type=Path, default=ROOT / "bench" / "config" / "tasks.json")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    harnesses = [name.strip() for name in args.harnesses.split(",")]
    models = [name.strip() for name in args.models.split(",")]
    registry = load_config("models")
    caps = load_config("harnesses")

    if args.tasks:
        tasks = [name.strip() for name in args.tasks.split(",")]
    else:
        manifest = json.loads(args.manifest.read_text())
        tasks = sorted(
            name
            for name, entry in manifest["tasks"].items()
            if (args.tier == "all" or entry.get("tier") == args.tier)
            and (not args.discriminating or entry.get("discriminates"))
        )
    builds_path = ROOT / "bench" / "config" / "builds.json"
    if builds_path.is_file():
        builds = json.loads(builds_path.read_text())
        failed = [task for task in tasks if builds.get(task, {}).get("status") not in (None, "ok")]
        if failed:
            print(f"tasks that failed the build gate: {failed}")
            return 2
    if not tasks:
        print("no tasks selected")
        return 2

    unknown = [name for name in models if name not in registry]
    if unknown:
        print(f"unknown model ids: {unknown}")
        return 2
    for model in models:
        for harness in harnesses:
            if harness not in registry[model]["harnesses"]:
                print(f"{model} is not supported on {harness} (supported: {registry[model]['harnesses']})")
                return 2

    concurrency = concurrency_cap(harnesses, models, registry, caps)
    manifest = json.loads(args.manifest.read_text()) if args.manifest.is_file() else {"tasks": {}}
    minutes = sum(
        (manifest["tasks"].get(task, {}).get("worst_p90") or 10.0) * args.attempts / concurrency
        for task in tasks
        for _ in harnesses
        for _ in models
    )

    run_id = f"{args.tag}-{args.attempts}a"
    command = (
        f"TASKS={','.join(tasks)} HARNESSES={','.join(harnesses)} MODELS={','.join(models)} "
        f"ATTEMPTS={args.attempts} CONCURRENCY={concurrency} RUN_ID={run_id} bash {RUNNER}"
    )
    print(f"tasks ({len(tasks)}): {','.join(tasks)}")
    print(f"jobs: {len(harnesses) * len(models)} | trials: {len(tasks) * args.attempts * len(harnesses) * len(models)}")
    print(f"concurrency: {concurrency} | estimated wall clock: {minutes / 60:.1f} h")
    print(command)
    if not args.execute:
        return 0

    env = dict(os.environ)
    env.update(
        TASKS=",".join(tasks),
        HARNESSES=",".join(harnesses),
        MODELS=",".join(models),
        ATTEMPTS=str(args.attempts),
        CONCURRENCY=str(concurrency),
        RUN_ID=run_id,
    )
    return subprocess.run(["bash", str(RUNNER)], env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
