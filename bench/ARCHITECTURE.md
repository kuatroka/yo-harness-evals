# Architecture

The system answers three questions about a moving target. Each has its own
protocol, and the order matters, because a harness verdict computed on an
unranked model measures the wrong thing.

## 1. Route check

Does a new harness, model, or effort level reach its provider and produce a
score at all? One task, one attempt, about six minutes. The question is binary
and the answer is a log line at 9Router plus one reward. Nothing here ranks
anything.

## 2. Ladder

Which model, and which effort within a model, is better? Fix the harness. Run
three to five fast discriminating tasks at n=3. Report per-task means, not a
pooled average, so a single saturated task cannot carry the result. Effort
ladders compare variants that share a `base`, which is why `models.json` stores
effort per row instead of treating effort as a dimension every harness must
understand.

## 3. Harness comparison

Which harness extracts more from the same model? Fix the model and the effort,
keep only tasks that still discriminate, and apply the decision rule. Do not run
this on a model you have not ranked first, because a harness advantage on a weak
model may be a harness advantage on fixing weak-model mistakes.

## Data model

Five files, one row per thing that can change:

| File | Written by | Holds |
|---|---|---|
| `config/harnesses.json` | hand | model-id template, concurrency cap, shim requirement |
| `config/models.json` | hand | route, base, effort, supported harnesses, pricing |
| `config/builds.json` | `screen.py` | whether the task's own Dockerfile builds, and how long |
| `config/tasks.json` | `calibrate.py` | measured p50/p90 per base, reward band, tier, liveness |
| `jobs/` | Harbor | the trial record, which is the only source of truth |

The shell reads models through `model_id.py`, so adding a model never edits
`run_matrix.sh`. Adding a harness still edits the launcher, because a harness is
a program with its own flags and its own auth path. That is the one branch that
earns its place.

The launcher also asks the registry whether a harness may run a model at all
(`model_id.py <key> supports <harness>`), so a pair outside its row is skipped
rather than silently running on whatever route the harness defaults to.

When one model is reachable two ways it gets two rows that share a `base`. Haiku
5.5 is the live case: `haiku-devin-max` reaches it through the Devin bridge for
Droid, while `haiku-anthropic-direct` reaches the same model with Claude
Code's own subscription token, no shim and no 9Router. Ladder tables group by
base, so the two rows line up. A harness comparison must not read across them
without saying so, because the endpoint is part of the treatment.

## Three gates before a task is used

1. Build. `docker build` with the task's own Dockerfile must pass. Three
   registry tasks failed this in one sitting, all inside `cargo fetch`, because
   their pinned 2024 toolchains cannot parse crates their lockfile-less repos
   resolve today. Nothing about a harness can be learned from that.
2. Cost. Worst-case p90 across model bases must sit under the configured
   ceiling, twenty minutes by default. The ceiling is a preference about
   turnaround, not a claim about what counts as a task. Worst case, not average, because a max-thinking model can be slower
   on a reasoning task than a small one.
3. Information. At least one base must score strictly between 0 and 1. A task
   that every model passes, or every model fails, is retired from ranking and
   kept for smoke.

Timed-out trials are excluded from the percentiles, since their duration
measures the budget that stopped them, and counted as failures in the score.
Censoring is by wall clock against the task's declared budget, not only by
Harbor's exception flag: a cancellation can be swallowed while the agent process
keeps streaming, and one Claude trial spent 25 minutes against a 600 second
budget and finished with no exception recorded.

The build gate is not theoretical. Of nine `swe-gen-rust` candidates screened in
one sitting, five built and four failed, all of them at the toolchain rather than
the task: a 2024 nightly that cannot parse today's crate manifests, and a 1.85
compiler that a resolved dependency no longer supports. Those four would have
burned a job slot each and taught nothing about a harness.

## Decision rule

A winner needs six or more discordant tasks in one direction, or a matched gap
of 0.10 with at least three shared tasks. Anything smaller is reported as a tie
and broken on p50 latency and list-price cost. The reason is arithmetic: a
binary reward at n=2 has a standard error of 0.35, so a two-trial cell cannot
distinguish a real difference from a coin flip, while latency and cost are
continuous and carry far more power per minute.

Cost is reported per priced trial, not per cell, because a harness whose token
counts go missing would otherwise look cheap by having fewer billed trials. On
the current record Droid prices about twice as high per trial as Amp or Claude
for the same reward on the same model. That is a hypothesis, not a verdict,
until both harnesses are shown to count cached tokens the same way.

## Budgets

The budget belongs to the task. Each trial runs on the `timeout_sec` declared in
the task's own `task.toml`, and a task that declares none gets Harbor's default
for each phase: no agent limit, ten minutes on the verifier, ten on the build,
six on agent setup. The bench
tasks declare none, so an agent runs until it stops. The matrix does not scale
those numbers and keeps no per-model multiplier, because that is how the
previous design ended up comparing fifteen minutes against twenty and calling it
capability.

Trials collected before the agent window was removed were stopped at ten
minutes. They stay in the store as censored trials: the raw mean leaves them out
and the timeout-counted mean reads them as zero, so a cell whose attempts all hit
that clock reports no usable mean rather than a fake one.

The cost gate is a selection rule, not a timeout. It decides which measured
tasks are cheap enough to spend model time on, and it never changes what a trial
is allowed to run.

## Recruitment loop

The pool is a population, not a fixture.

1. Pull a candidate dataset with `harbor download`.
2. Read its metadata filter (difficulty, category) and shortlist.
3. `screen.py` builds the shortlist.
4. Run two trials, one on the slowest base and one on the strongest.
5. `calibrate.py` assigns a tier and a band. Promote what lands under the
   ceiling with a live band. Retire what saturates.

Retirement is what keeps the benchmark honest as models improve. A static task
list silently becomes a list of solved tasks.

The first three recruited tasks measured like this: `alacritty-3921` separated
the models (swe-2 0.667, DeepSeek 1.0), `ripgrep-2576` passed everywhere (6 of
6), and `ripgrep-2555` failed everywhere (0 of 6). One in three carried
information. All three failed the cost gate with p90 between 8.4 and 10.9
minutes, and eight of eighteen trials hit the 600-second cap, three of them after
the fix had already landed. A Rust workspace compiles and tests inside the trial,
so that pool cannot supply an eight-minute tier no matter how hard the bugs are.
Difficulty and duration have to be screened together. The uncapped reruns of the
capped `alacritty` trials scored higher in several cells, so those screening
scores are a floor for the capped pool, not a ranking.

`ripgrep-2555` is the floor check. At n=2 every model and harness pair scores
0.00 on it, including the fast ones, so a pass there would mean something broke
in the verifier rather than that a harness improved.

Haiku 5.5 on the Devin route produced no measurement. All eight trials came back
`502` with `Devin stream error failed_precondition: Your weekly usage quota has
been exhausted`, from the first request. Droid retried until the ten-minute
budget ended each trial; Amp surfaced the error and exited in under four
minutes. Those zeros describe the route, not the model, so the row cannot rank
Haiku until the quota resets. This is the case the guardrail exists for: an
Anthropic model whose own access works should not be measured through a bridge
that can refuse it.

A native Droid route was tried first and does not exist on this account. Droid
1.15.0 rejects `claude-haiku-5-5` as an invalid model, its catalog lists only
`claude-haiku-4-5-20251001`, and Factory inference on the host answers `402
no_active_subscription`. `--list-tools` accepts any id and prints a friendly
label, so it is not evidence that a model exists; only `droid exec` is.

The direct Anthropic row measured Claude Code at max effort: one of two
`alacritty` attempts passed in 8.9 minutes, and the other ran 22.8 minutes
without a cap and failed. Both `ripgrep-2555` attempts finished in five to seven
minutes and failed. The same cell measured before the effort flag was wired in
scored 0 of 2 in two and a half minutes, so the level changed both the answer and
the clock by roughly four times. `haiku-amp-native` puts Amp on its own Claude
access at the same effort, so the harness comparison holds the route fixed. Its
first valid cell passed both `alacritty` attempts at max effort (12.1M and 9.6M
input tokens) and failed both `ripgrep` attempts, with tokens recorded on all
four. The same mode run before the flag was wired in scored zero on its only
graded trial.

## Cadence

Nightly, run the smoke tier on the newest model to catch route breakage within a
day. Weekly, recalibrate, retire saturated tasks, and recruit replacements. Per
model release, run the ladder. Per harness release, run the harness comparison on
whatever model currently leads.
