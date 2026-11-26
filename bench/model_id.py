"""Read one value out of the model registry, for the shell to consume.

The matrix used to keep its own ``case`` table of model ids, which meant every
new model was a bash edit in three places. This makes the registry the single
source and keeps the shell dumb.

    bench/model_id.py 9r/dv-bridge/swe-2-max route     # devin/swe-2-max
    bench/model_id.py 9r/dv-bridge/swe-2-max effort    # max
    bench/model_id.py keys                # 9r/cx/gpt-6-luna-max,...
    bench/model_id.py efforts             # high,max,xhigh
    bench/model_id.py 9r/dv-bridge/swe-2-max supports claude   # yes (exit 0); no exits 1
    bench/model_id.py check               # every schema problem, exit 1 if any
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import load_config, validate_models  # noqa: E402

FIELDS = ("route", "effort", "base", "provider", "max-concurrency")


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "check":
        problems = validate_models(load_config("models"))
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    if len(argv) == 2 and argv[1] == "keys":
        print(",".join(sorted(load_config("models"))))
        return 0
    if len(argv) == 2 and argv[1] == "efforts":
        efforts = {spec["effort"] for spec in load_config("models").values()}
        print(",".join(sorted(efforts)))
        return 0
    if len(argv) == 4 and argv[2] == "supports":
        key, harness = argv[1], argv[3]
        models = load_config("models")
        if key not in models:
            print(f"unknown model: {key}", file=sys.stderr)
            return 2
        supported = key in models and harness in models[key].get("harnesses", [])
        print("yes" if supported else "no")
        return 0 if supported else 1
    if len(argv) != 3 or argv[2] not in FIELDS:
        print(f"usage: {argv[0]} <key> [{'|'.join(FIELDS)}] | keys | efforts | check", file=sys.stderr)
        return 2
    key, field = argv[1], argv[2]
    models = load_config("models")
    if key not in models:
        print(f"unknown model: {key}", file=sys.stderr)
        return 2
    if field == "max-concurrency":
        # Zero means the row sets no model-level limit.
        print(models[key].get("max_concurrency", 0))
        return 0
    print(models[key][field])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
