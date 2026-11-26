# Selected tasks

The default matrix uses five tasks from `frontier-harness-eval/frontier-harness-eval@v1.1.0`. All five:

- use a glibc-based Debian or Ubuntu image
- allow internet access
- have a Dockerfile that builds without private resources or credentials
- have a 900-second agent timeout and a 900-second verifier timeout
- fit the available disk and do not need a GPU

| Task | Category | Difficulty | Agent timeout | Image base | Workdir |
|---|---|---|---:|---|---|
| `regex-log` | data-processing | medium | 900 s | `ubuntu:24.04` | `/app` |
| `nginx-request-logging` | system-administration | medium | 900 s | `python:3.13-slim-bookworm` | `/app` |
| `polyglot-c-py` | software-engineering | medium | 900 s | `ubuntu:24.04` | `/app` |
| `openssl-selfsigned-cert` | security | medium | 900 s | `python:3.13-slim-bookworm` | `/app` |
| `sqlite-db-truncate` | debugging | medium | 900 s | `python:3.13-slim-bookworm` | `/app` |

## Hard-task model run

The three-model max run uses two hard tasks:

| Task | Category | Difficulty | Expert / junior estimate | Agent timeout | Image base |
|---|---|---|---:|---:|---|
| `feal-linear-cryptanalysis` | mathematics | hard | 960 / 19,200 min | 1,800 s | `python:3.13-slim-bookworm` |
| `sparql-university` | data-querying | hard | 800 / 10,000 min | 900 s | `ubuntu:24.04` |

These are the hardest pair that keeps the 12-cell run bounded. `fix-ocaml-gc` has the highest expert estimate (1,440 min), but its 3,600-second timeout would make the run roughly twice as long. Both selected tasks are glibc-based, allow internet access, need no GPU, and build from self-contained Dockerfiles. The verifiers install their own Python dependencies with `uvx`.

## Why these five

- They cover different work: regex construction, service configuration, a C/Python polyglot, TLS certificate generation, and SQLite recovery.
- They exercise both reading and writing the task environment. A harness that only edits files but cannot run shell commands will struggle.
- They are short enough for an early signal. The full dataset includes many 8-hour tasks and heavy ML workloads.
- Their images are small compared with the ML tasks, which matters on a disk that is already fairly full.
- They are from the `alexgshaw/*` subset with explicit `allow_internet = true`, so harness installation has a better chance of working.
- Their Dockerfiles are self-contained. The scripts force-build them for the host architecture, which is required for native Droid and Amp binaries on Apple Silicon.

`regex-log` is the intended smoke test. It is a small, single-file task, so a failure there points at plumbing rather than task difficulty.

## What was excluded

- Tasks with an 8-hour agent timeout (`28800 s`) were excluded from the first pass. They are useful later, but they make a failed adapter expensive to diagnose.
- Tasks with no explicit `allow_internet = true` were excluded. Harness install and model calls need egress.
- GPU tasks and large ML/data tasks were excluded because of disk and runtime cost.
- Alpine/musl images were excluded because Droid and Amp need glibc.
- `fix-git` was excluded from the force-build matrix. Its Dockerfile clones a private GitHub repository in `setup.sh`, so the build fails without credentials. The published prebuilt image is x86_64, and the x86_64 Droid binary segfaults under Rosetta on Apple Silicon.

## Adding a task

1. Confirm the task's `task.toml` has `allow_internet = true`.
2. Inspect `environment/Dockerfile` and confirm the base is Debian or Ubuntu, or another glibc distribution.
3. Check `agent.timeout_sec` and `verifier.timeout_sec`. Start with tasks under one hour.
4. Confirm the Dockerfile builds without private repositories, credentials, or host-only resources.
5. Confirm the task does not need a GPU.
6. Add the task name to the `TASKS` list in `scripts/run_matrix.sh`, or pass it explicitly:

```bash
TASKS=regex-log,my-new-task ./scripts/run_matrix.sh
```

The task name is the directory under `datasets/frontier-harness-eval/`. The smoke script passes that directory with `-p`; the matrix passes the dataset with `-p` and filters the task with `-i`.

## Caveats

Five tasks and one attempt are enough to validate the plumbing and get an early signal. They are not enough for a statistically meaningful comparison between Droid and Amp. Increase the task count and use multiple attempts before drawing conclusions about harness quality.
