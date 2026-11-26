# yo-harness-evals

Local [Frontier Harness](https://frontierharness.org/evaluate-your-own-harness/) runs of three coding harnesses against the same tasks, models and container images, so their scores are comparable to each other.

| Harness | Agent | Model access |
|---|---|---|
| Factory Droid | `harnesses.droid:DroidAgent` | 9Router over `host.docker.internal:20128` |
| Amp Code | `harnesses.amp:AmpAgent` | 9Router through Amp's server-side connection |
| Claude Code | `harnesses.claude:ClaudeCodeAgent` | 9Router Anthropic endpoint through the identity shim, or Anthropic direct for `haiku-anthropic-direct` |

Model keys are `swe-2-max`, `swe-2-flash-max`, `swe-2-1-xhigh`, `swe-2-1-max`, `deepseek-engy-max`, `haiku-devin-max`, `haiku-amp-native` and `haiku-anthropic-direct`. A Devin route encodes effort in the model ID. A direct Anthropic id carries none, so `run_matrix.sh` passes `reasoning_effort` from the registry and the adapter launches the CLI with `--effort max`; without that flag the model runs at its default level. Droid relies on the custom model ID, which spells the effort; Amp passes `--model` plus `--effort max`. A model label carries effort only when the route encodes it: the jobs table turns a trailing `-max` into `(max)` (`viewer_overlay/display.py`), and a direct Anthropic id has no suffix. For those rows read the trial's `reasoning_effort`, which the Claude adapter also copies into agent metadata as `effort`.

| Key | Model id | Served by |
|---|---|---|
| `swe-2-max` | `devin/swe-2-max` | local Devin bridge |
| `swe-2-flash-max` | `devin/swe-2-flash-max` | local Devin bridge |
| `swe-2-1-xhigh` | `devin/swe-2-1-xhigh` | local Devin bridge, Droid, Amp, Claude, Codex |
| `swe-2-1-max` | `devin/swe-2-1-max` | local Devin bridge, Droid, Amp, Claude, Codex |
| `deepseek-engy-max` | `engy/deepseek-v4.1-flash-1017(max)` | Engy, direct from 9Router |
| `haiku-devin-max` | `devin/haiku-5-5-max` | local Devin bridge, Droid's only Haiku 5.5 route (paused: Devin weekly quota) |
| `haiku-amp-native` | `anthropic/claude-haiku-5-5` | Amp's own Claude access, no 9Router |
| `haiku-anthropic-direct` | `claude-haiku-5-5` | Claude Code's own subscription token, Anthropic direct |

**Anthropic models go to the harness's own access first.** For fable, opus, sonnet or haiku, the default is the route the harness ships with. Claude Code uses the subscription token from the host keychain, straight to `api.anthropic.com`, with no shim and no 9Router. Droid has no Anthropic access of its own on this account: its catalog has no Haiku 5.5 and Factory inference answers 402 `no_active_subscription`, so the Droid Haiku row is the Devin bridge and inherits that route's quota risk. Amp uses its own Claude models, `anthropic/claude-haiku-5-5` and the rest, with `--effort max` on the CLI and `reasoningEffort` in the mode definition Amp's plugin API documents. Route an Anthropic model through 9Router or the Devin bridge only when that route is asked for explicitly; a bridged Anthropic model still gets its own registry row, so the results show which endpoint produced the score.

## Setup

macOS or Linux, Docker Desktop running, [`uv`](https://docs.astral.sh/uv/), Python 3.12.

```bash
uv sync
cp .env.example .env
uv run harbor download frontier-harness-eval/frontier-harness-eval@v1.1.0 --export -o datasets
```

`.env` needs `NINEROUTER_API_KEY` for Droid and for Claude Code when it runs through the shim. Amp needs `AMP_API_KEY` (an `sgamp_` access token). Claude Code on an Anthropic model uses the host's own Claude Code login instead, read from the macOS keychain, so no key goes in `.env` for that route. Droid uses `FACTORY_API_KEY` when set and otherwise copies the host login from `~/.factory`. Task images must be glibc based; Alpine and musl are refused.

## Run

```bash
./scripts/run_matrix.sh
```

Devin's guide caps swe-2 at two concurrent calls. Both swe-2 rows carry `max_concurrency: 2` in `bench/config/models.json`, and `run_matrix.sh` and `plan.py` take the narrowest cap across the selected harnesses and model rows, so a wider harness cannot overrun a narrower model. Defaults: 5 smoke tasks, `droid,amp`, **2 attempts**, **2 retries**, concurrency 1. Override with `TASKS`, `MODELS`, `HARNESSES`, `ATTEMPTS`, `RETRIES`, `CONCURRENCY`, `DRY_RUN=1`.

The matrix does not set timeouts, and the tasks in `datasets/bench-fast/` declare none either. Harbor has a default for each phase: no limit on the agent at all, ten minutes on the verifier, ten on the environment build, and six on agent setup, which is the CLI install. A task that declares its own `timeout_sec` runs on exactly that number, so the frontier tasks with fifteen- and thirty-minute agent windows keep them. There is no scaling knob and no per-model window.

Fast hard-task pilot, about 15 minutes of agent time per attempt:

```bash
TASKS=fix-code-vulnerability,cancel-async-tasks,sparql-university \
MODELS=swe-2-max,swe-2-flash-max HARNESSES=droid,amp,claude ./scripts/run_matrix.sh
```

Add `claude` to `HARNESSES` deliberately. It installs the CLI in the container with apt and bun. Requests to a non-Anthropic model go through `scripts/claude_identity_shim.py` on port 20140, which the matrix starts and stops on its own. The shim removes the three paragraphs of Claude Code's prompt that Devin's content policy rejects and forwards everything else untouched. On an Anthropic model the CLI talks to `api.anthropic.com` with the host's token and no shim, per the routing rule above. Effort comes from the model ID rather than a kwarg, so the matrix runs `scripts/backfill_metadata.py` afterward to record it for the viewer.

## Reading the results

A **task** is one benchmark item. A **trial** is one task run by one agent, ending in one verifier reward. A **job** is one invocation over a task set with one agent and model. An **attempt** is one execution of a task inside a job, multiplied by `ATTEMPTS`.

The job mean is the average trial reward and estimates pass@1. `pass_at_k` answers whether at least one of k attempts succeeded. Only means from the same attempt count belong in one ranking.

Each trial carries `reward_valid`, set from its exception type. Timeouts and nonzero exits keep a valid reward, and that reward can be `1.0` if the work landed before the agent process was cut off. Docker, sandbox, download and API failures have no valid reward and are dropped from the metric, which silently shrinks the denominator. Check `reward_valid`, never the error count, before trusting a mean.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/run_matrix.sh` | Runs the jobs, starts and stops the Claude Code shim, backfills metadata |
| `scripts/claude_identity_shim.py` | Strips the three prompt paragraphs Devin's policy rejects, forwards everything else |
| `scripts/backfill_metadata.py` | Records the reasoning effort the viewer reads, derived from the model ID when no kwarg carries it |
| `scripts/replace_trial.py` | Swaps a rerun into a finished job, archives the trial it replaced under `archive/replaced-trials/`, recomputes the job stats |

Replacing a trial changes the job mean, so a rerun of failed trials is no longer pass@1 for that model. Run the whole task set again when you need a comparison that holds.

For a comparison that scales past a handful of models, use `bench/`. Registries in `bench/config/` hold the harnesses, model variants, and measured task metadata; `calibrate.py` regenerates the task manifest from finished trials, `report.py` applies the pre-registered verdict rule, and `plan.py` turns a selection into a job. See [bench/README.md](bench/README.md).

## Errors and fixes

| Symptom | Cause | Fix |
|---|---|---|
| `Agent 'amp-code' does not support reasoning_effort` | Agent advertises no capabilities | `capabilities = AgentCapabilities(reasoning_effort=True)` in both adapters |
| Provider column shows `harbor-skip-parsing` | `--skip-model-parsing` makes Harbor label every trial | `to_agent_info()` override in `droid.py`, `amp.py`, `claude.py` |
| Source, Agents, Models are dashes in the viewer | Job-level `config.json` missing | Rebuild it from a sibling job, not from the trials |
| `RuntimeError` reaching `colima/ci/docker.sock` | Stale Docker context points at a dead socket | `colima delete --profile ci --force`, then confirm `docker context ls` |
| `NonZeroAgentExitCodeError`, log says provider internal error | Transient Devin 5xx | Rerun the trial; keep the reward if one was recorded |
| `AgentTimeoutError` | Task budget exhausted | Not infrastructure. It scores `0.0` and stays |
| A Claude trial runs long after its budget, sometimes with no exception at all | Harbor cancels with `asyncio.wait_for`, but the streaming CLI can swallow the cancellation; the trial ends when the process finally exits | `bench/` censors any trial whose agent phase outlasts a task that declares a budget. A task with no budget has nothing to censor against, so the run is simply long |
| `NonZeroAgentExitCodeError` with exit 143 and an empty agent log | Something sent SIGTERM to the agent process. Not the agent timeout, which raises `AgentTimeoutError` instead | Seen once, on `install-windows-3.11`, after ten minutes of normal model traffic. Reward stays valid, so it counts as a real `0.0` |
| Install fails on Alpine or musl | Droid and Amp ship glibc binaries | Use a Debian or Ubuntu task image |
| Claude Code gets 401 or model-not-found | Needs the Anthropic Messages endpoint and key | `--base-url .../v1` plus `ANTHROPIC_API_KEY`, not the OpenAI format |
| Claude Code retries forever, Devin returns `permission_denied` content policy | Devin rejects three paragraphs of Claude Code's own prompt: the Anthropic identity line, the security-policy paragraph, and the Claude vendor paragraph. Tools, metadata, thinking and the task prompt are innocent | Handled by `scripts/claude_identity_shim.py`, which the matrix starts on port 20140 and points Claude Code at |

## Viewer

```bash
./scripts/harbor_view.sh                 # http://127.0.0.1:8080
```

The overlay in `viewer_overlay/` imports Harbor's own `create_app` and splices its routes in front of the SPA fallback. It serves `/launch` for a new run or a rerun, and rewrites agent names, model labels and token totals in API responses. Nothing under `site-packages` is edited, so a Harbor upgrade cannot revert it.

`/launch` picks a dataset, a harness, a model key that harness can route, an effort and one or more tasks. A rerun prefills from the source job's `config.json`, including the dataset that job used, and reports every field it cannot resolve instead of guessing. Launching shells out to `scripts/run_matrix.sh`, the same path a terminal run takes.

The one exception to leaving `site-packages` alone is the compiled bundle's Total column. `scripts/patch_viewer_columns.sh` adds it, and `tests/test_harbor_upgrade_guard.py` fails with the re-patch command after an upgrade replaces those assets.

`uv run harbor view jobs` still serves Harbor's own viewer, without the overlay. Give it another port with `PORT=8081 uv run harbor view jobs` while the overlay owns 8080.

Older incident detail, including the 9Router timeout, Bun idle-limit and stale-tunnel work, is archived in [docs/readme-history-20261120.md](docs/readme-history-20261120.md).
