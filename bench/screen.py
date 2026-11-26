"""Screen candidate tasks by building their environment before any agent runs.

The first three registry tasks recruited for the fast tier all failed inside
``cargo fetch``: their Dockerfiles pin a 2024 toolchain, the repos commit no
lockfile, and cargo resolves crate versions it cannot parse. Each failure cost a
job slot and told us nothing about harnesses. A task that cannot build is not a
task, so the build is a gate with its own recorded result.

    .venv/bin/python bench/screen.py datasets/bench-fast
    .venv/bin/python bench/screen.py datasets/bench-fast --timeout 900
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent / "config" / "builds.json"


def screen(task_dir: Path, timeout: int) -> dict:
    environment = task_dir / "environment"
    if not (environment / "Dockerfile").is_file():
        return {"status": "missing-dockerfile", "seconds": 0.0}
    started = time.time()
    try:
        completed = subprocess.run(
            ["docker", "build", "-t", f"bench-screen/{task_dir.name}", str(environment)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "seconds": round(time.time() - started, 1)}
    seconds = round(time.time() - started, 1)
    if completed.returncode == 0:
        return {"status": "ok", "seconds": seconds}
    tail = [line for line in completed.stderr.strip().splitlines() if line.strip()][-3:]
    return {"status": "failed", "seconds": seconds, "tail": tail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="directory of task directories")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    results = json.loads(args.out.read_text()) if args.out.is_file() else {}
    for task_dir in sorted(path for path in args.dataset.iterdir() if path.is_dir()):
        result = screen(task_dir, args.timeout)
        results[task_dir.name] = {**result, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        print(f"{task_dir.name:34s} {result['status']:18s} {result['seconds']:6.1f}s")
    args.out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    ok = sum(1 for entry in results.values() if entry["status"] == "ok")
    print(f"\n{ok}/{len(results)} build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
