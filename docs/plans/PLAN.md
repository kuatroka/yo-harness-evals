# Plan

## Objective

Evaluate a small subset of the Frontier Harness tasks with two harnesses that Harbor does not ship:

- Factory Droid
- Amp Code

Use one model, `devin/deepseek-v4.1-flash`, at two reasoning efforts, `high` and `max`.

The full matrix is:

```text
5 tasks x 2 harnesses x 2 efforts x 1 attempt = 20 trials
```

This is a full-matrix configuration, not a default smoke run. Run all 20 cells only with explicit approval. The leaderboard uses four attempts; a four-attempt local run would be 80 trials.

## Scope

In scope:

- Install Harbor in a project-local virtual environment.
- Write two Harbor `BaseInstalledAgent` adapters.
- Install each harness inside a task container.
- Configure the model route and reasoning effort.
- Select five compatible tasks.
- Provide smoke and matrix scripts.
- Document credentials, results, and limitations.

Out of scope:

- Reproducing the leaderboard's exact sandbox, egress policy, or model serving.
- Publishing an official leaderboard score.
- Modifying Frontier Harness tasks or verifiers.
- Running the full 62-task dataset.
- Baseten or Runta integration.

## Architecture

```text
Harbor
  |
  +-- Docker task container
        |
        +-- Factory Droid adapter
        |     install Node 22 + droid@1.13.1
        |     write ~/.factory/harbor-settings.json (mode 600)
        |     run droid exec --skip-permissions-unsafe ...
        |     |
        |     +--> http://host.docker.internal:20128/v1
        |           9Router -> Local Devin bridge (127.0.0.1:20130)
        |             -> devin/deepseek-v4.1-flash-{high,max}
        |                devin/swe-2-max
        |                devin/swe-2-flash-max
        |             -> server.codeium.com (Devin)
        |             or -> api.engy.ai when engy-override.json maps the model
        |
        +-- Amp adapter
              install Amp CLI
              write settings + fh-modes.ts plugin
              run amp --mode 'DV DS Flash 4.1 high' ...
              |
              +--> Amp API -> Amp servers -> 9Router
                   -> Local Devin bridge
                   -> devin/deepseek-v4.1-flash-{high,max}
```

## Design decisions

### Use `BaseInstalledAgent`, not `BaseAgent`

`BaseInstalledAgent` installs and runs the harness inside the sandbox. `BaseAgent` would run the loop on the host and require a custom tool bridge into the container. The installed-agent route is the intended Harbor abstraction and keeps the agent close to the task.

### Use custom adapters, not ACP

Both `factory-droid` and `amp-acp` exist in the ACP registry, but Harbor's ACP runner does not appear to pass arbitrary config such as Amp's effort setting. Clean ACP containers also lack the user's custom model configuration, and `amp-acp` still needs the Amp CLI installed separately. The custom adapters are smaller and more direct.

### Encode Droid effort in the model ID

Host tests showed that `droid exec -r/--reasoning-effort` does not control effort for the custom `devin/...` models. The working model IDs encode effort:

```text
custom:devin/deepseek-v4.1-flash-high
custom:devin/deepseek-v4.1-flash-max
```

### Select the model explicitly

Both adapters accept a `model` key in addition to `effort`. The supported values are `deepseek-flash-high`, `deepseek-flash-max`, `swe-2-max`, and `swe-2-flash-max`. The scripts pass the key with `--ak model=...`; the adapter maps it to the Droid custom ID or the Amp mode. The SWE-2 variants are max-only. This keeps the effort and model dimensions separate without duplicating the adapter command logic.

### Use an Amp mode plugin

Amp's model routing is account-side. The adapter writes a local plugin that registers four modes: DeepSeek high/max plus SWE-2 max and SWE-2 Flash max, using the `devin/...` model IDs and the desired `reasoningEffort`. The user's `9Router local` connection maps those IDs to `devin/...`.

### Treat the bridge as part of the model route

The 9Router `Local Devin bridge` connection forwards `devin/...` requests to a host service at `127.0.0.1:20130`. That service can reroute a model to Engy through `~/.config/devin-bridge/engy-override.json`. 9Router still attributes the request to the Devin bridge connection, so its usage table cannot prove the upstream provider. Check the bridge override before treating `devin/...` as a Devin result.

### Raise the setup timeout

Both adapters set `DEFAULT_SETUP_TIMEOUT_SEC = 1200` instead of Harbor's 360-second default. The scripts force-build task images for the host architecture. On Apple Silicon this produces arm64 images, so Node and Droid install natively; if a build falls back to x86_64 emulation, the longer ceiling still applies. This only affects setup, not the task's 900-second agent timeout.

### Keep secrets out of Git and out of command lines

- `.env` is ignored.
- Droid's 9Router key is uploaded into a mode-600 settings file.
- The adapter does not echo the key.
- Amp's token is passed to the Amp process through its environment.

## Work breakdown

### 1. Runtime and scaffold

- [x] Inspect target directory.
- [x] Confirm Docker is running.
- [x] Scaffold `pyproject.toml`, `.python-version`, `.gitignore`.
- [x] `uv sync` and install Harbor 0.26.0.
- [x] Write `harnesses/common.py`.

### 2. Factory Droid adapter

- [x] Implement `harnesses/droid.py`.
- [x] Install Node 22 and `droid@1.13.1`.
- [x] Generate the minimal custom-model settings file.
- [x] Upload it with mode 600.
- [x] Run `droid exec` in the task workdir.
- [x] Validate imports and dry-run.
- [x] Confirm a clean container has no host login and needs either `FACTORY_API_KEY` or copied host auth.
- [x] Confirm arm64 install and execution with a dummy key, ending in a clean auth error.
- [x] Verify the host's `auth.v2.file` + `auth.v2.key` authenticate in an arm64 Linux container and complete a model call.
- [x] Add automatic host-auth fallback to `DroidAgent`.
- [x] Complete an end-to-end Droid smoke trial with host auth and no Factory key: reward `1.0` on `regex-log`, 2m46s.

### 3. Amp adapter

- [x] Implement `harnesses/amp.py`.
- [x] Install the Linux Amp CLI.
- [x] Write `amp.dangerouslyAllowAll: true`.
- [x] Write the two-mode plugin.
- [x] Run `amp --mode ... -x ...`.
- [x] Validate imports and dry-run.
- [x] Confirm arm64 install, settings, plugin, and mode invocation with a dummy key, ending in a clean auth error.
- [x] Complete an end-to-end Amp smoke trial with a valid Amp token: reward `1.0` on `regex-log`, 2m56s.
- [x] Confirm real plugin loading and a valid high-effort model call.
- [x] Confirm the max mode loads, but note that the bridge rerouted the max matrix cells to Engy; see the routing risk.

### 4. Configuration and scripts

- [x] Add `.env.example`.
- [x] Add `scripts/run_smoke.sh`.
- [x] Add `scripts/run_matrix.sh`.
- [x] Make scripts executable.
- [x] Validate a five-task dry run.

### 5. Dataset and tasks

- [x] Download `frontier-harness-eval/frontier-harness-eval@v1.1.0`.
- [x] Inspect all 62 task metadata files.
- [x] Select five glibc, network-enabled, force-buildable tasks with 900-second timeouts.
- [x] Exclude `fix-git` because its Dockerfile clones a private GitHub repository.
- [x] Document the selection in `TASKS.md`.

### 6. Validation

- [x] Adapter imports.
- [x] Single-task dry run for Droid.
- [x] Single-task dry run for Amp.
- [x] Five-task dry run.
- [x] Full 20-trial dry run for both harnesses and both efforts.
- [x] Build all five selected task images for arm64 from their Dockerfiles.
- [x] Confirm native arm64 Droid runs and x86_64 Droid segfaults under Rosetta.
- [x] Confirm arm64 Droid and Amp install and invoke their commands with dummy keys.
- [x] Droid smoke trial on `regex-log` with host auth and no Factory key: reward `1.0`, job `smoke-droid-high-regex-log-20261118-003204`.
- [x] Amp smoke trial on `regex-log` with a valid `AMP_API_KEY` access token: reward `1.0`, job `smoke-amp-high-regex-log-20261118-004446`.
- [x] Full 20-trial matrix completed: 20/20 reward `1.0`, mixed-provider (10 high cells on Devin, 10 max cells on Engy via the bridge override). The max cells are provisional.
- [ ] Optional max-only rerun after the routing fix, only with explicit approval.
- [ ] Optional 80-trial matrix at four attempts.

### 7. Hard-task model run

- [x] Choose two hard tasks with bounded runtime: `feal-linear-cryptanalysis` and `sparql-university`.
- [x] Add explicit `model` selection for `deepseek-flash-max`, `swe-2-max`, and `swe-2-flash-max` to both adapters.
- [x] Dry-run the 12-cell configuration.
- [x] Preflight both SWE-2 models through 9Router with no Engy tap or stream.
- [x] Run 2 tasks x 2 harnesses x 3 models (12 cells), run `models-20261118-153618`.
- [x] Record rewards and routing evidence; all 12 original cells produced valid rewards.
- [x] Diagnose and fix the later Amp SWE-2 502s: 9Router 60 s fetch timeout, 255 s Bun idle limit, and a stale localtunnel subdomain.
- [x] Validate the fixes with Amp `swe-2-max` on `feal-linear-cryptanalysis`: reward `1.0`, 0 errors, 5m40s, job `smoke-amp-swe-2-max-feal-linear-cryptanalysis-20261119-010445`.
- [x] Repeat the three-model Droid/Amp setup on `regex-log` as an infrastructure check: 6/6 reward `1.0`, 0 errors, 109 requests all on the Local Devin bridge, run `three-models-regex-20261119-012911`.
- [x] Re-run the `ApiInternalServerError` (502) hard-task trials and delete the errored jobs: `infra-rerun-20261119-020103` scored 2/2 `1.0` on both Amp `swe-2-max` and `swe-2-flash-max`, 0 errors. Eight 502 job dirs removed; `AgentTimeoutError`/auth/exit-code jobs kept as non-infrastructure failures.

## Validation gates

A trial counts as a successful plumbing test when:

1. Harbor creates the task container.
2. The adapter installs the harness without manual intervention.
3. The harness makes an authenticated model call.
4. The harness modifies the task environment.
5. The verifier runs and produces a score.
6. The result is visible in the Harbor job output.

A smoke test that merely installs the harness but never reaches the model is not enough.

## Risks

- **Docker host route:** `host.docker.internal:20128` works from the arm64 task container; the Droid smoke trial reached 9Router and completed a model call.
- **Droid identity:** a clean container has no browser login. The adapter uses `FACTORY_API_KEY` when set; otherwise it copies the host's `auth.v2.file` + `auth.v2.key` into the container. The host-auth path was verified in an arm64 Linux container. Its risk is session expiry or revocation; re-run Droid on the host to refresh. The 9Router key is separate and still required.
- **Rosetta:** the x86_64 Droid binary segfaults under Rosetta on Apple Silicon. The scripts force-build arm64 task images to avoid it. Setting `FORCE_BUILD=0` on Apple Silicon will use the x86_64 prebuilt images and reintroduce the crash.
- **Private build resources:** some task Dockerfiles clone private repositories. `fix-git` was excluded for this reason. A new task with a private setup will fail under `--force-build`.
- **Amp token:** the host's interactive Amp session is not a valid headless token. `AMP_API_KEY` must be an `sgamp_...` access token from Settings → Security, not the `amp login` session token.
- **Amp account routing:** the `9Router local` connection's base URL is account-side state and must match the live public tunnel. Amp's servers cannot reach a dead localtunnel host. The current URL is `https://9router-yomac-v1-20261119-7c3f2a.loca.lt/v1`; update it with `amp config model-providers edit-router` if the tunnel hostname changes. A `502` at the public host means the edge is stale, while `401` means it reached 9Router.
- **Long-stream ceiling:** Bun's `Bun.serve` maximum `idleTimeout` is 255 seconds. The bridge now switches to SSE after 20 seconds and sends keepalives every 10 seconds; 9Router's fetch timeout is 360 seconds. Do not remove the early-stream path without replacing both limits.
- **Silent bridge reroute:** the local `devin-bridge` can reroute a mapped model to Engy through `~/.config/devin-bridge/engy-override.json`. 9Router records the request against the Local Devin bridge either way. An override created after Devin's daily quota was exhausted sent the 10 max matrix cells to Engy. The override was backed up and emptied on 2026-11-18; one max check then returned `ROUTE_CHECK_DEVIN` with no Engy tap or stream. Recheck the file before any provider comparison. If Devin quota is exhausted, max now fails loudly.
- **Plugin loading:** the high-effort Amp smoke loaded `fh-modes.ts` and completed a model call. The max mode also loaded, but its matrix cells were rerouted to Engy. If a later trial reports the plugin is not ready, retry or add a readiness wait.
- **Hard-task runtime:** `feal-linear-cryptanalysis` has a 1,800-second agent limit and `sparql-university` has 900 seconds. A 12-cell run can take hours; a timeout is not a harness failure without inspecting the trajectory.
- **SWE-2 model support:** `devin/swe-2-max` and `devin/swe-2-flash-max` both returned HTTP 200 through 9Router with no Engy tap or stream. The Amp account connection maps `devin/swe-2-*` to those `devin/...` models; the 12-cell run and the post-fix `swe-2-max` validation both routed successfully.
- **Disk:** the machine is about 87% full with roughly 237 GB free. The selected images are small, but job logs and layers accumulate. Do not prune unrelated Docker data.
- **Comparability:** local results are not leaderboard-comparable. Document this whenever the numbers are shared.
- **Statistical power:** five tasks and one attempt are a plumbing and early-signal run, not a harness ranking.

## Open questions

- Resolved: Amp's max mode loaded and reached the account's `9Router local` connection, but the local bridge rerouted the max matrix cells to Engy. The max cells are not Devin max results.
- Resolved: the copied Droid host session remained valid for the full 20-trial matrix.
- Remaining: if a valid max comparison is required, rerun only the max cells after the routing fix, with explicit approval. The 10 high cells already used Devin.
- Remaining: which task failures are harness behavior versus task difficulty? This matrix had no failures, so harder tasks or more attempts are needed.
- Decided: do not add a synthetic one-step ATIF fallback, because it would enable Phoenix's trace control without representing the real LLM/tool trajectory. Add real ATIF event emission only if trace-level analysis becomes a requirement.
- Remaining: Phoenix Cloud retires on 2026-11-30. Self-hosted Phoenix remains supported; Arize AX has no first-party Harbor plugin. The Phoenix Cloud upload path itself is now validated.
- Resolved: both Droid and Amp DeepSeek-max scored 1.0 on both hard tasks. Droid SWE-2 max scored 0.5 (Feal `AgentTimeoutError`, Sparql 1.0); Droid SWE-2 Flash max scored 0.5 (Feal 0.0, Sparql 1.0). Amp SWE-2 max scored 0.5 (Feal 0.0, Sparql 1.0); Amp SWE-2 Flash max scored 0.0 on both. All 12 original cells are valid task results; the later retry/final jobs are excluded infrastructure 502s.
- Resolved: the 502s were caused by a 9Router 60 s fetch timeout with four attempts, a 255 s Bun idle ceiling on silently buffered Devin turns, and a stale localtunnel subdomain. The fixes are a 360 s 9Router timeout, bridge SSE keepalives after 20 s, and a new fixed tunnel hostname reflected in Amp.
- Remaining: a genuine Devin upstream timeout of roughly 300 s can still produce a provider-side 502. That is not the fixed local routing class and needs a Devin-side or request-shaping fix if it recurs.

## Current status

Adapters and scripts are implemented. Harbor dry runs pass, including the full 20-trial configuration. All five selected Dockerfiles build for arm64. Both harnesses install and execute on arm64; with dummy credentials they stop at a clean authentication error, not a crash. A real Droid smoke trial on `regex-log` with host auth and no Factory key passed with reward `1.0` in 2m46s. Droid no longer needs a Factory API key locally. A real Amp smoke trial on `regex-log` with the `sgamp_` access token passed with reward `1.0` in 2m56s.

The full 20-trial matrix finished with reward `1.0` in all 20 cells, but the result is provisional and mixed-provider. The 10 high cells used the Devin path; the 10 max cells were rerouted to Engy by `~/.config/devin-bridge/engy-override.json`. The override has been backed up and emptied, the bridge restarted, and one max check returned `ROUTE_CHECK_DEVIN` with no Engy tap or stream. The max cells are not valid Devin max results and should be rerun only with explicit approval. The matrix was larger than the requested run; do not launch it again by default.

The 12-cell hard-task run `models-20261118-153618` finished. It covers `feal-linear-cryptanalysis` and `sparql-university` across Droid and Amp with `deepseek-v4.1-flash-max`, `swe-2-max`, and `swe-2-flash-max`. The adapters accept an explicit model key, the Amp plugin registers four modes, and both SWE-2 models preflighted through 9Router without an Engy tap or stream. Droid DeepSeek max and Amp DeepSeek max scored 1.0 on both tasks. Droid SWE-2 max and Droid SWE-2 Flash max each scored 0.5; Amp SWE-2 max scored 0.5 and Amp SWE-2 Flash max scored 0.0. All 12 original cells are valid task results. The later retry/final 502 jobs are excluded.

The 502 investigation found three infrastructure failures, all fixed on 2026-11-19: 9Router's 60 s fetch timeout with four attempts, a 255 s Bun idle ceiling on buffered Devin turns, and a stale localtunnel subdomain. 9Router now has a 360 s timeout; the bridge early-streams with SSE keepalives after 20 s; and Amp's `9Router local` connection points at the new tunnel `https://9router-yomac-v1-20261119-7c3f2a.loca.lt/v1`. A fresh Amp `swe-2-max` trial on `feal-linear-cryptanalysis` passed with reward `1.0`, 0 errors, and 5m40s (`smoke-amp-swe-2-max-feal-linear-cryptanalysis-20261119-010445`). A genuine Devin upstream timeout near 300 s remains possible and is a separate provider issue.

The Phoenix Harbor plugin is installed. Local Phoenix contains the completed Amp DeepSeek experiment. Phoenix Cloud contains the uploaded completed jobs. The dataset `frontier-harness-eval` has verifier rewards, `infra_ok`, and latency. The custom adapters do not emit ATIF, so the runs have no output or trace; full Phoenix tracing needs an ATIF producer. Phoenix Cloud is retiring on 2026-11-30; self-hosted Phoenix is the durable path.
