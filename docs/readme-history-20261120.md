# yo-harness-evals

Local [Frontier Harness](https://frontierharness.org/evaluate-your-own-harness/) evaluations with two custom Harbor adapters:

- **Factory Droid** (`harnesses.droid:DroidAgent`)
- **Amp Code** (`harnesses.amp:AmpAgent`)

The model is `devin/deepseek-v4.1-flash` at `high` and `max` reasoning effort. The full matrix is five tasks, two harnesses, two efforts, one attempt: 20 trials. Run the full matrix only when you intend to run all 20 cells; a smoke trial is one cell.

This repository is a measurement tool, not a leaderboard submission. A local score is real, but it is not automatically comparable to the public leaderboard because the sandbox, model serving path, egress policy, harness versions, and audit conditions differ.

## Status

- Harbor 0.26.0 is installed in a local `.venv`.
- Both adapters import and pass Harbor dry-run validation.
- The full 20-trial configuration resolves in a dry run.
- All five selected Dockerfiles build for arm64 from source.
- Droid and Amp install and execute on arm64. With dummy credentials they stop at a clean authentication error, not a crash. The x86_64 Droid binary segfaults under Rosetta, so the scripts force-build task images for the host architecture.
- Droid no longer requires a Factory API key for local runs. If `FACTORY_API_KEY` is unset, the adapter copies the host CLI's `~/.factory/auth.v2.file` and `auth.v2.key` into the container. This authenticated and completed a real model call in an arm64 Linux test.
- Amp requires an access token (`sgamp_`) from Settings → Security. The short-lived `amp login` session token is rejected by design in non-interactive runs.
- A real Droid smoke trial on `regex-log` with host auth and no Factory key passed with reward `1.0` in 2m46s (`jobs/smoke-droid-high-regex-log-20261118-003204`).
- A real Amp smoke trial on `regex-log` with the `sgamp_` access token passed with reward `1.0` in 2m56s (`jobs/smoke-amp-high-regex-log-20261118-004446`). The mode plugin loaded, the mode was accepted, and the model call routed through Amp.
- The full 20-trial matrix finished with reward `1.0` in all 20 cells. The result is provisional and mixed-provider: the 10 `high` cells used the Devin path, while the 10 `max` cells were rerouted to Engy by a local bridge override. The max cells are not valid evidence for a Devin max comparison. See [Model routing](#model-routing).
- A 12-cell hard-task max run finished: `feal-linear-cryptanalysis` and `sparql-university` across Droid and Amp, using `deepseek-v4.1-flash-max`, `swe-2-max`, and `swe-2-flash-max`. The run ID is `models-20261118-153618`; all 12 cells produced valid rewards. Later retry jobs hit infrastructure 502s; those were traced to a 60-second 9Router fetch timeout with four attempts, long silent Devin turns crossing the 255-second Bun idle limit, and a stale localtunnel subdomain. The fixes are documented under [Model routing](#model-routing). A fresh post-fix Amp `swe-2-max` trial solved `feal-linear-cryptanalysis` with reward `1.0` (`jobs/smoke-amp-swe-2-max-feal-linear-cryptanalysis-20261119-010445`).
- The Phoenix Harbor plugin is installed (`arize-phoenix-client 3.7.0`), and `scripts/upload_phoenix.py` imports completed jobs. Local Phoenix validation passed for a completed Amp job: `frontier-harness-eval` contains an experiment with two runs, `reward` and `infra_ok` scores, and latency; the custom adapters currently emit no ATIF, so Phoenix has no run output or trace for those trials.
- The same job was uploaded successfully to Phoenix Cloud at `https://app.phoenix.arize.com/s/<workspace-handle>`; the experiment has two runs with `reward=1.0` and `infra_ok=1.0`. Phoenix Cloud retires on 2026-11-30; self-hosted Phoenix remains supported.

## Layout

```text
.
├── harnesses/
│   ├── common.py          # shared container helpers
│   ├── droid.py           # Factory Droid adapter
│   └── amp.py             # Amp Code adapter
├── scripts/
│   ├── run_smoke.sh       # one task, one harness, one effort
│   ├── run_matrix.sh      # five tasks, both harnesses, both efforts
│   └── upload_phoenix.py  # upload completed jobs to Phoenix
├── datasets/              # downloaded tasks, gitignored
├── jobs/                  # Harbor job output, gitignored
├── logs/                  # console logs from the scripts, gitignored
├── .env.example
├── TASKS.md               # selected tasks and selection criteria
└── PLAN.md                # architecture, gates, risks, and decisions
```

## Prerequisites

- macOS or Linux
- Docker Desktop or Docker Engine, running
- [`uv`](https://docs.astral.sh/uv/)
- Python 3.12 (the project pins `>=3.12,<3.14`)
- Network access from the host and from task containers
- About 10 GB of free disk for the selected images and jobs

The selected tasks use glibc-based Debian or Ubuntu images. Droid and Amp both ship glibc-linked Linux binaries, so Alpine/musl task images are not supported.

The scripts pass `--force-build`, so task images are built from their Dockerfiles for the host architecture. On Apple Silicon this produces arm64 images and avoids the x86_64 Droid segfault under Rosetta. On x86_64 it rebuilds the same task source; Docker layer caching makes later runs cheap. Set `FORCE_BUILD=0` to use the published prebuilt images instead, which is only recommended on x86_64.

Both adapters allow 20 minutes for agent setup. Native arm64 builds are fast; if you force x86_64 images under emulation, installs can be slow. This setup ceiling does not change the task's own agent timeout.

## Setup

```bash
cd /Users/yo_macbook/Documents/dev/yo-harness-evals

uv sync

cp .env.example .env
# Droid: fill in NINEROUTER_API_KEY. The host Droid login is used automatically
# unless FACTORY_API_KEY is set. Amp: fill in AMP_API_KEY with an sgamp_ token.

set -a
source .env
set +a
```

`.env` is ignored by Git. Do not commit it. The scripts also source `.env` automatically when it exists.

## Credentials

### Droid: 9Router key

`NINEROUTER_API_KEY` is the key for the local 9Router. The adapter writes it into a private settings file inside the task container with mode `600`. It is not passed on the command line.

The default model base URL is:

```text
http://host.docker.internal:20128/v1
```

That route works from a Docker Desktop container on macOS when 9Router is listening on the host at `127.0.0.1:20128`. Verify it before a full run:

```bash
docker run --rm curlimages/curl:latest \
  -sS -m 20 -o /dev/null -w '%{http_code}\n' \
  http://host.docker.internal:20128/v1/models
```

A `200` means the container can reach the router. For Linux Docker Engine, `host.docker.internal` may not resolve; use the host gateway address or set `DROID_MODEL_BASE_URL` to another reachable URL. For Podman, `host.containers.internal` is the usual route.

### Droid: Factory identity

A clean container has no browser login, so the CLI does not see the login you use in the host terminal. The adapter supports two ways to provide a Factory identity:

1. **Host login (default, no API key).** If `FACTORY_API_KEY` is unset, the adapter copies the host CLI's current `~/.factory/auth.v2.file` and `~/.factory/auth.v2.key` into the task container with mode `600`. This authenticated and completed a real model call in an arm64 Linux container test. If the host login expires, re-run `droid` on the host to refresh it; the adapter copies the files at `configure()` time. Set `DROID_HOST_AUTH_DIR` if the files are not under `~/.factory`.
2. **Factory API key.** If you have a Factory API key, set `FACTORY_API_KEY=fk-...`; the adapter passes it to the container and does not copy the host login. This is the documented path for CI and headless environments. Create one at [app.factory.ai/settings/api-keys](https://app.factory.ai/settings/api-keys).

The host-login path is a local convenience. It uses your existing Factory session and may stop working when that session expires or is revoked. The 9Router key is still required for inference; it is separate from the Factory identity. Treat the auth files as credentials: the adapter uploads them mode `600` and does not log their contents.

### Amp: access token

`AMP_API_KEY` must be an Amp access token that starts with `sgamp_`. Create one at [Amp Settings → Security → Access tokens](https://ampcode.com/settings/security#access-token). See [Amp execute mode](https://ampcode.com/docs/cli/execute-mode).

This is an access token, not a paid API key. The saved interactive Amp session on the host is not a substitute: the CLI rejects the short-lived session token that `amp login` stores, because it expires within an hour and cannot be refreshed from an environment variable.

Amp's model requests go from Amp's servers to the model connection configured on the Amp account. The `9Router local` connection on the account maps:

```text
devin/deepseek-v4.1-flash-high -> devin/deepseek-v4.1-flash-high
devin/deepseek-v4.1-flash-max  -> devin/deepseek-v4.1-flash-max
```

The adapter registers four Amp modes with those `devin/...` IDs. The container does not need the 9Router key for Amp, because Amp stores the connection server-side. The account connection and its tunnel must be live before the trial. The `devin/...` request then reaches the local Devin bridge, which makes the final provider choice; see [Model routing](#model-routing).

### Effort mapping

| Effort | Droid model ID | Amp mode | Upstream model |
|---|---|---|---|
| high | `custom:devin/deepseek-v4.1-flash-high` | `DV DS Flash 4.1 high` | `devin/deepseek-v4.1-flash-high` |
| max | `custom:devin/deepseek-v4.1-flash-max` | `DV DS Flash 4.1 max` | `devin/deepseek-v4.1-flash-max` |

Droid's effort is encoded in the custom model ID. The adapter does not use `--reasoning-effort` for these models because the host tests showed it does not control their effort.

The adapters also accept an explicit model key. This is how the hard-task run selects the three max models:

| Model key | Droid custom ID | Amp mode | 9Router model |
|---|---|---|---|
| `deepseek-flash-max` | `custom:devin/deepseek-v4.1-flash-max` | `DV DS Flash 4.1 max` | `devin/deepseek-v4.1-flash-max` |
| `swe-2-max` | `custom:devin/swe-2-max` | `DV SWE-2 max` | `devin/swe-2-max` |
| `swe-2-flash-max` | `custom:devin/swe-2-flash-max` | `DV SWE-2 Flash max` | `devin/swe-2-flash-max` |

Pass `--ak model=<key>` to either adapter. The scripts expose this as the fourth argument to `run_smoke.sh` or the `MODELS` environment variable to `run_matrix.sh`. All three keys above are max-effort models.

## Model routing

The `devin/...` model IDs reach 9Router's **Local Devin bridge** connection, whose base URL is `http://127.0.0.1:20130/v1`. The bridge itself is a separate host service at `~/Documents/dev/devin-bridge/src/droid-bridge.ts`. It makes the final provider choice, so the 9Router usage table is not proof of the upstream provider: 9Router records the request against the Local Devin bridge connection either way.

The bridge reads an optional override at `~/.config/devin-bridge/engy-override.json`. If that file maps a model ID, the bridge sends that model to Engy instead of Devin. The bridge's source is not part of this repository, so the override is host environment state rather than versioned project state.

An override created on 2026-11-08 mapped only `deepseek-v4.1-flash-max` to Engy after Devin reported its daily quota exhausted. That is why the max-effort matrix cells appeared in Engy while 9Router attributed them to the Devin bridge. The `high` model was never in the override and took the Devin path.

The override was backed up to `~/.config/devin-bridge/engy-override.json.bak-20261118-0200` and replaced with `{}` on 2026-11-18. The bridge was restarted with:

```bash
launchctl kickstart -k gui/$(id -u)/com.local.devin.droid-bridge
```

One max-effort check through 9Router then returned `ROUTE_CHECK_DEVIN` with HTTP 200. The bridge log showed the access line but no `engy stream`, and no `/tmp/bridge-engy-req-*.json` tap changed. If Devin quota is exhausted again, max now fails with a Devin error instead of silently falling back to Engy.

For the 12-cell matrix window (2026-11-18 15:30-18:15), 9Router's usage table recorded 599 requests against the **Local Devin bridge** connection: 231 `deepseek-v4.1-flash-max`, 354 `swe-2-flash-max`, and 14 `swe-2-max`. The Engy connection had 0 requests in that window. The bridge override was `{}` throughout.

To audit a route, check the override file, the bridge log at `/tmp/devin-droid-bridge.log`, and the tap files:

```bash
cat ~/.config/devin-bridge/engy-override.json
rg 'engy stream|devin deepseek' /tmp/devin-droid-bridge.log
ls -lt /tmp/bridge-engy-req-*.json
```

A Devin call should add an `access` line but no new `engy stream` line and no new tap mtime. A successful Devin call does not log its model name, because the bridge only logs routing failures and Engy streams. Use the Devin dashboard to confirm billing.

### Amp public route and the SWE-2 502 incident

Amp sends model requests from its own servers, so the Amp route is public even though 9Router and the bridge are local:

```text
Amp servers
  -> https://9router-yomac-v1-20261119-7c3f2a.loca.lt/v1
  -> v1-only proxy 127.0.0.1:20131
  -> 9Router 127.0.0.1:20128
  -> Local Devin bridge 127.0.0.1:20130
  -> server.codeium.com
```

The Amp connection is `9Router local` (`e40734b6-e1f5-4581-b860-45a33890fda7`). Its base URL is account-side state, not a repository file. Inspect or change it with:

```bash
amp config model-providers show e40734b6-e1f5-4581-b860-45a33890fda7 --json \
  | jq -r '.config.baseURL'
amp config model-providers edit-router e40734b6-e1f5-4581-b860-45a33890fda7 \
  --base-url https://9router-yomac-v1-20261119-7c3f2a.loca.lt/v1
```

The 502s had three separate causes:

1. **9Router timeout.** The default `FETCH_CONNECT_TIMEOUT_MS` is 60 seconds, and its 502 policy makes three retries. Four attempts plus backoff produced a 502 after about 249 seconds, logged as `[502]: fetch connect timeout`. The failing requests never reached the bridge. 9Router now runs under `com.local.9router.server` with `FETCH_CONNECT_TIMEOUT_MS=360000`; 70-second and 270-second slow-mock calls returned `200`.
2. **Silent long turns.** The bridge originally buffered the whole Devin response, and Bun's `Bun.serve` has a maximum `idleTimeout` of 255 seconds. A 242-second turn succeeded, but the next long turn could be cut off in the bridge, proxy, or tunnel. The bridge now waits 20 seconds for a fast buffered result and otherwise switches to an SSE response with a `: keepalive` comment every 10 seconds. Fast failures still return a real 502. The public tunnel forwards the keepalives, verified with a live `swe-2-flash-max` request.
3. **Stale localtunnel subdomain.** The old fixed hostname `9router-yomac-v1-20261026-b4e91d.loca.lt` was still held by the localtunnel server and returned `502 Bad Gateway` at the edge, before the request reached the proxy. That is why the failed trial had no new 9Router request. The tunnel job now uses `9router-yomac-v1-20261119-7c3f2a.loca.lt`, and Amp's base URL was updated to match. An unauthenticated request to the new host returns `401` (reachable), not `502`.

A genuine Devin upstream timeout of roughly 300 seconds can still produce a provider-side 502; that is distinct from the three fixed infrastructure failures. The passing validation trial had two 69-148 second model turns and zero errors.

Operational checks:

```bash
# 401 means the tunnel reached 9Router; 502 means the edge is stale.
curl -sS -o /dev/null -w '%{http_code}\n' \
  https://9router-yomac-v1-20261119-7c3f2a.loca.lt/v1/models

launchctl print gui/$(id -u)/com.local.9router.server
launchctl print gui/$(id -u)/com.local.9router.public-tunnel
launchctl print gui/$(id -u)/com.local.devin.droid-bridge
tail -n 40 /tmp/9router-public-tunnel.log /tmp/9router-v1-proxy.log /tmp/devin-droid-bridge.log
```

The tunnel supervisor checks the public `/v1/models` URL every 60 seconds and restarts the localtunnel client after two consecutive responses other than `200` or `401`. If the hostname changes again, update the Amp base URL with `edit-router` before rerunning a trial.

## Dataset

The public dataset is:

```text
frontier-harness-eval/frontier-harness-eval@v1.1.0
```

Download it into the gitignored `datasets/` directory:

```bash
uv run harbor download \
  frontier-harness-eval/frontier-harness-eval@v1.1.0 \
  --export -o datasets
```

The selected five tasks are in [TASKS.md](TASKS.md). They are all glibc-based and network-enabled, with 900-second agent and verifier timeouts. Their Dockerfiles are the build source; the scripts force-build them for the host architecture.

## Run

### One smoke trial

Droid, high effort, `regex-log`:

```bash
./scripts/run_smoke.sh droid high regex-log
```

Amp, high effort, `regex-log`:

```bash
./scripts/run_smoke.sh amp high regex-log
```

Droid, max effort, `regex-log`, explicit SWE-2 model:

```bash
./scripts/run_smoke.sh droid max regex-log swe-2-max
```

The script loads `.env`, checks the credential for the selected harness, runs one attempt, and writes both a Harbor job and a console log under `logs/`.

Use `DRY_RUN=1` to validate the command without pulling images or running a trial:

```bash
DRY_RUN=1 ./scripts/run_smoke.sh droid high regex-log
```

### Full matrix

```bash
./scripts/run_matrix.sh
```

Defaults:

- tasks: `regex-log,nginx-request-logging,polyglot-c-py,openssl-selfsigned-cert,sqlite-db-truncate`
- harnesses: `droid,amp`
- efforts: `high,max`
- attempts: `1`
- concurrency: `1`

Override any of them with environment variables:

```bash
HARNESSES=droid ./scripts/run_matrix.sh
ATTEMPTS=4 CONCURRENCY=2 ./scripts/run_matrix.sh
TASKS=regex-log,sqlite-db-truncate ./scripts/run_matrix.sh
MODELS=deepseek-flash-max,swe-2-max,swe-2-flash-max \
  TASKS=feal-linear-cryptanalysis,sparql-university \
  ./scripts/run_matrix.sh
DEBUG=1 ./scripts/run_matrix.sh
DRY_RUN=1 HARNESSES=droid ./scripts/run_matrix.sh
```

`MODELS` creates one job per harness and model. The model key determines the effort, so `EFFORTS` is only used when `MODELS` is empty.

If `AMP_API_KEY` is not set, run `HARNESSES=droid`; Droid can use the host login. The matrix script refuses to start a harness whose credentials are missing.

The matrix uses four Harbor jobs, one per harness and effort, with five tasks in each. It runs them sequentially. Each job gets a timestamped name under `jobs/`, and a console log under `logs/`.

## Results

The full matrix from `matrix-20261118-004804` finished. Every cell scored reward `1.0`:

| Job | Provider path | Reward | Runtime |
|---|---|---|---:|
| `fh-droid-high-matrix-20261118-004804` | Devin | 5/5 | 15m12s |
| `fh-droid-max-matrix-20261118-004804` | Engy (override; not a Devin max result) | 5/5 | 14m50s |
| `fh-amp-high-matrix-20261118-004804` | Devin | 5/5 | 11m59s |
| `fh-amp-max-matrix-20261118-004804` | Engy (override; not a Devin max result) | 5/5 | 22m52s |

The 20/20 total is provisional. The 10 high cells used the requested Devin route; the 10 max cells did not. Do not present the 20/20 as a harness ranking, and do not treat the max cells as a Devin max comparison. A valid max comparison requires rerunning the max cells after the routing fix, with explicit approval.

### Hard-task max run

`models-20261118-153618` is the hard-task capability run:

- tasks: `feal-linear-cryptanalysis` and `sparql-university`
- harnesses: Droid and Amp
- models: `deepseek-v4.1-flash-max`, `swe-2-max`, and `swe-2-flash-max`
- cells: 2 tasks x 2 harnesses x 3 models = 12

The original run finished. Both SWE-2 models were preflighted through 9Router before launch: each returned HTTP 200 and neither changed the Engy tap or emitted an Engy stream.

| Harness | Model | feal | sparql | Status |
|---|---|---|---:|---|
| Droid | `deepseek-v4.1-flash-max` | 1.0 | 1.0 | valid |
| Droid | `swe-2-max` | 0.0 (`AgentTimeoutError`) | 1.0 | valid reward, agent timeout |
| Droid | `swe-2-flash-max` | 0.0 | 1.0 | valid |
| Amp | `deepseek-v4.1-flash-max` | 1.0 | 1.0 | valid |
| Amp | `swe-2-max` | 0.0 | 1.0 | valid |
| Amp | `swe-2-flash-max` | 0.0 | 0.0 | valid |

All 12 original cells produced valid rewards. The later `retry2` and `final` jobs are infrastructure failures, not task results, and are excluded from any harness ranking. They were caused by the 9Router timeout, Bun idle limit, and stale tunnel described in [Model routing](#model-routing), not by Amp or Droid setup.

Post-fix validation:

| Job | Harness | Model | Task | Reward | Runtime |
|---|---|---|---|---|---:|
| `smoke-amp-swe-2-max-feal-linear-cryptanalysis-20261119-010445` | Amp | `swe-2-max` | `feal-linear-cryptanalysis` | 1.0 | 5m40s |

That trial had `n_errors: 0`, two long model turns of 69 s and 148 s, and no 502. It is a single trial, not a replacement for the 12-cell matrix.

### Three-model `regex-log` infrastructure check

`three-models-regex-20261119-012911` repeats the three-model setup on the easiest task, `regex-log`, to check the repaired route rather than model capability:

- harnesses: Droid and Amp
- models: `deepseek-v4.1-flash-max`, `swe-2-max`, `swe-2-flash-max`
- cells: 2 harnesses x 3 models = 6
- attempts: 1 per cell

| Harness | `deepseek-v4.1-flash-max` | `swe-2-max` | `swe-2-flash-max` |
|---|---|---|---|
| Droid | 1.0 (3m37s) | 1.0 (2m34s) | 1.0 (3m34s) |
| Amp | 1.0 (3m42s) | 1.0 (2m45s) | 1.0 (5m35s) |

All six jobs had `n_errors: 0` and no exceptions. The run window made 109 9Router requests, all against the Local Devin bridge, with no Engy requests and no 502/timeout entries. Because `regex-log` is the smoke task, the uniform 1.0 scores are evidence that the route works, not evidence that the three models are equivalent on harder tasks.

### Infra-error re-run

`infra-rerun-20261119-020103` re-ran the trials that had failed with `ApiInternalServerError` (502 model inaccessibility). The original errored jobs are not task results; after the re-runs produced valid rewards they were deleted from `jobs/` and `logs/`.

| Job | Amp `swe-2-max` | Amp `swe-2-flash-max` |
|---|---|---|
| feal-linear-cryptanalysis | 1.0 | 1.0 |
| sparql-university | 1.0 | 1.0 |

Both jobs scored 2/2 reward `1.0`, `n_errors: 0`, 133 requests all through the Local Devin bridge, no Engy, no 502. The Amp `swe-2-max` feal result improved from `0.0` (original matrix) to `1.0`, and Amp `swe-2-flash-max` improved from `0.0/0.0` to `1.0/1.0`. Deleted as infrastructure failures, not task results: `fh-amp-swe-2-{max,flash-max}-{retry2,final}-20261118-*` (6 dirs), `fh-amp-swe-2-max-rerun-20261118-181533`, and `smoke-amp-swe-2-max-feal-linear-cryptanalysis-20261119-004430`. Kept because their errors were agent/task-level, not model inaccessibility: `fh-droid-swe-2-max-models-20261118-153618` (`AgentTimeoutError`, has a valid sparql 1.0) and the four early `smoke-*` auth/exit-code/setup failures.

Harbor writes job data under `jobs/<job-name>/`. The console output includes each trial's result and the job summary. Use Harbor's viewer to inspect trajectories:

```bash
uv run harbor view jobs/<job-name>
```

The adapters also write the harness output to `/logs/agent/droid.txt` or `/logs/agent/amp.txt` inside the container. Harbor copies agent logs into the job directory.

## Phoenix results viewer

Harbor's built-in viewer is enough to inspect a single job. Phoenix adds a persistent dataset and experiment layer across jobs, with one experiment per harness/model, verifier rewards as evaluations, and each ATIF trajectory as a trace.

```bash
uv add "arize-phoenix-client[harbor]"
uv run harbor plugins list   # should show arize-phoenix
```

Phoenix connection settings belong in `.env`:

```bash
PHOENIX_API_KEY=...
PHOENIX_ENDPOINT=https://app.phoenix.arize.com/s/<workspace-handle>
PHOENIX_COLLECTOR_ENDPOINT=https://app.phoenix.arize.com/s/<workspace-handle>
PHOENIX_PROJECT=default
```

For a local Phoenix server, use `http://localhost:6006` for both endpoint variables and omit the API key. A bare `https://app.phoenix.arize.com` is not a valid Phoenix Cloud base URL; the workspace path must include `/s/<handle>`.

Run a new job with the plugin:

```bash
uv run harbor run ... --plugin arize-phoenix --yes
```

Import a completed job after the fact:

```bash
uv run python scripts/upload_phoenix.py jobs/<job-name> --dry-run
uv run python scripts/upload_phoenix.py jobs/<job-name>
```

The plugin maps a Harbor job to a Phoenix dataset, one experiment per resolved agent/model configuration, one run per trial, verifier rewards to named evaluations, and the ATIF trajectory to a `harbor.trial` trace with AGENT, LLM, and TOOL spans. It does not replace Harbor's artifact tree, verifier stdout, `config.json`, or lock-file views.

These custom adapters currently write only `amp.txt`/`droid.txt`. They do not emit the canonical `agent/trajectory.json` ATIF file, so Phoenix records the dataset, experiments, runs, latency, and scores but not the agent output or a trace. The experiment's **View trace** control is disabled. Full Phoenix tracing requires adding ATIF emission to the adapters. A synthetic single-step fallback was not added because it would enable the control without representing the actual LLM and tool trajectory.

Phoenix's `infra_ok` is derived from Harbor's exception field. A trial that ends with `AgentTimeoutError` gets `infra_ok=0.0` even when Harbor's policy grades it as a valid reward, as happened on Droid `swe-2-max` / `feal-linear-cryptanalysis`. Check the exception type, not only the label, when comparing runs.

Phoenix Cloud retires on 2026-11-30. Phoenix itself remains free and self-hosted, and that is the durable path for this plugin. Arize AX is a separate product with separate `ak-` credentials; I found no AX-native Harbor or ATIF integration in the AX docs. AX would require a custom exporter.

## How the adapters work

Both adapters extend Harbor's `BaseInstalledAgent`:

- `install()` runs once during environment setup.
- `configure()` writes container-specific settings and plugins after install.
- `run()` executes the harness against the task instruction in the task workdir.

### Droid

`harnesses/droid.py`:

1. Installs `curl`, `bash`, `git`, `tar`, `xz`, and CA certificates.
2. Installs Node 22.23.5 into `/usr/local` if Node 20+ is not already present.
3. Installs `droid@1.13.1` globally with auto-update disabled.
4. Writes `~/.factory/harbor-settings.json` with four custom models (DeepSeek high/max and the two SWE-2 max variants), the 9Router base URL, and the 9Router key. The file is uploaded with mode `600`.
5. If `FACTORY_API_KEY` is unset, copies the host's `~/.factory/auth.v2.file` and `auth.v2.key` into the container with mode `600`. If the key is set, it passes the key in the process environment instead.
6. Runs:

```bash
droid exec \
  --settings ~/.factory/harbor-settings.json \
  --model custom:devin/deepseek-v4.1-flash-high \
  --skip-permissions-unsafe \
  --cwd <task-workdir> \
  --output-format json \
  "<instruction>"
```

`--output-format json` emits a `result` event whose `usage` object carries `input_tokens`, `output_tokens`, `cache_read_input_tokens`, and `cache_creation_input_tokens`. The adapter normalizes them into Harbor's `AgentContext` (`n_input_tokens` = input + cache read + cache creation, `n_cache_tokens` = cache read + cache creation) in `populate_context_post_run`.

`--skip-permissions-unsafe` is intentional. The task container is disposable and the benchmark expects the agent to modify the environment.

### Amp

`harnesses/amp.py`:

1. Installs the Linux Amp CLI from `https://ampcode.com/install.sh`.
2. Writes `~/.config/amp/settings.json` with `amp.dangerouslyAllowAll: true`.
3. Writes `~/.config/amp/plugins/fh-modes.ts`, which registers four modes: DeepSeek high/max plus `DV SWE-2 max` and `DV SWE-2 Flash max`, using the corresponding `devin/...` model IDs.
4. Runs:

```bash
amp --mode 'DV DS Flash 4.1 high' --stream-json -x '<instruction>'
```

`--stream-json` emits per-turn `assistant.message.usage` events (`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `output_tokens`). The adapter sums them into Harbor's `AgentContext` with the same normalization as Droid. Plain `-x` output has no usage, so token accounting requires this flag.

Amp's mode plugin is the cleanest way to carry both the model and the reasoning effort. The adapter does not pass a separate `--effort` because the mode already encodes it.

## What is and is not comparable

This setup measures the same task instructions and verifier logic as the public Frontier Harness dataset, but the surrounding conditions are local:

- The container runtime is Docker on this machine, not the leaderboard sandbox.
- Task images are rebuilt from the task Dockerfiles for the host architecture, rather than using the published prebuilt images. On x86_64 this should be equivalent; on Apple Silicon the arm64 build is necessary for native harness binaries.
- Model serving goes through the local 9Router and its Devin bridge for both harnesses. Amp's servers forward to the account's 9Router connection, and the bridge makes the final provider choice. The 9Router usage table does not show that final choice.
- Egress policy and network latency differ.
- Harness versions are pinned here and may differ from the leaderboard.
- The default matrix uses one attempt, while the leaderboard uses multiple attempts.
- The hard-task run uses one attempt on tasks with 900- and 1,800-second agent limits. A timeout is not a harness failure without inspecting the trajectory.

Treat the output as a controlled local comparison of two harnesses on the same five tasks. Do not present it as an official leaderboard score.

## Troubleshooting

### Docker is not running

Start Docker Desktop and confirm:

```bash
docker info
```

### Droid cannot reach the model

Check the container route:

```bash
docker run --rm curlimages/curl:latest \
  -sS -m 20 -o /dev/null -w '%{http_code}\n' \
  http://host.docker.internal:20128/v1/models
```

If that fails, set `DROID_MODEL_BASE_URL` to a reachable URL. For a public tunnel, use the tunnel's `/v1` URL and the same 9Router key.

### A model routes to the wrong provider

Check the bridge override first:

```bash
cat ~/.config/devin-bridge/engy-override.json
```

`{}` means no override. If a model ID is present, the bridge sends that model to Engy instead of Devin. Back up the file, remove the entry, and restart the bridge:

```bash
cp -p ~/.config/devin-bridge/engy-override.json \
  ~/.config/devin-bridge/engy-override.json.bak
printf '{}\n' > ~/.config/devin-bridge/engy-override.json
launchctl kickstart -k gui/$(id -u)/com.local.devin.droid-bridge
```

9Router's usage table attributes both paths to the Local Devin bridge, so confirm the upstream with the bridge log, the tap files, or the provider dashboard.

### Droid says it is not authenticated

Check that the host Droid login is still valid:

```bash
droid doctor --checks auth
```

The adapter uses `FACTORY_API_KEY` when set. Otherwise it copies `~/.factory/auth.v2.file` and `~/.factory/auth.v2.key` into the container. If those files are missing or expired, log in to Droid on the host again, or set `FACTORY_API_KEY` if your plan provides one. `DROID_HOST_AUTH_DIR` can point at a different auth directory. The 9Router key only authenticates inference; it is not a Factory identity.

### Droid exits with 139 or a segfault

The x86_64 Droid binary segfaults under Rosetta on Apple Silicon. Run with `FORCE_BUILD=1` (the default) so the task image is arm64 and the arm64 Droid binary runs natively. Confirm the container architecture:

```bash
docker run --rm <task-image> uname -m
```

It should print `aarch64` on Apple Silicon. If you are on an Intel host, the x86_64 binary should run natively.

### Amp rejects the token

`AMP_API_KEY` must be an `sgamp_...` access token, not a saved interactive session token. Create one at [Settings → Security → Access tokens](https://ampcode.com/settings/security#access-token) and retry.

### Amp cannot find the mode

Check the job's agent log for plugin loading errors. The plugin is uploaded to `~/.config/amp/plugins/fh-modes.ts`. If Amp reports that the plugin is not ready, retry the trial; plugin registration is asynchronous.

### An Alpine or musl task fails during install

That is expected. Droid and Amp need glibc. Use Debian or Ubuntu task images.

### Disk pressure

The selected images are small, but Docker layers, job logs, and trajectories accumulate. Monitor:

```bash
docker system df
df -h
```

Do not prune images or containers that belong to another project. The Harbor jobs in `jobs/` are this project's output and can be archived or removed when no longer needed.

## Version pins

| Component | Version |
|---|---|
| Harbor | 0.26.0 |
| Factory Droid | 1.13.1 |
| Node.js | 22.23.5 |
| Amp | latest at install time |

## References

- [Frontier Harness: evaluate your own harness](https://frontierharness.org/evaluate-your-own-harness/)
- [Frontier Harness dataset](https://hub.harborframework.com/datasets/frontier-harness-eval/frontier-harness-eval)
- [Harbor installation](https://docs.harborframework.com/getting-started/installation)
- [Harbor custom agents](https://docs.harborframework.com/agents/custom-agents)
- [Harbor agent auth resolution](https://docs.harborframework.com/agents/agent-auth-resolution)
- [Factory Droid Exec](https://docs.factory.com/droid-exec/overview)
- [Factory custom models](https://docs.factory.com/docs/model-independence/byok)
- [Amp execute mode](https://ampcode.com/docs/cli/execute-mode)
- [Amp model selector](https://ampcode.com/docs/model-selector)
