"""Shared data layer for the bench framework.

Every number the framework reports is derived from Harbor's own ``result.json``
files, so a report can be recomputed without rerunning a single trial. The unit
of record is one finished trial. A job that has not finished is skipped, because
its partial trial set would silently bias a comparison.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent / "config"

HARNESS_ALIASES = {
    "factory-droid": "droid",
    "harnesses.droid:DroidAgent": "droid",
    "amp-code": "amp",
    "harnesses.amp:AmpAgent": "amp",
    "claude-code": "claude",
    "harnesses.claude:ClaudeCodeAgent": "claude",
    "codex": "codex",
    "harbor.agents.installed.codex:Codex": "codex",
}

TIMEOUT_EXCEPTION = "AgentTimeoutError"


@dataclass(frozen=True)
class Trial:
    job: str
    harness: str | None
    model: str | None
    task: str
    reward: float | None
    timed_out: bool
    duration_min: float | None
    finished: bool
    tokens_in: int | None = None
    tokens_cached: int | None = None
    tokens_out: int | None = None


@dataclass(frozen=True)
class Cell:
    """One harness/model/task slot, scored two ways."""

    n_usable: int
    n_timeout: int
    mean_without_timeouts: float | None
    mean_with_timeouts: float | None
    p50_minutes: float | None
    p90_minutes: float | None


def load_config(name: str) -> dict:
    return json.loads((CONFIG_DIR / f"{name}.json").read_text())


def resolve_model(observed: str | None, models: dict) -> str | None:
    """Map a model id as Harbor recorded it onto a key in models.json."""
    if not observed:
        return None
    name = observed.split("custom:", 1)[-1]
    candidates = [name]
    if name.startswith("dv/"):
        candidates.append("devin/" + name.split("/", 1)[1])
    matches = [key for key, spec in models.items() if spec["route"] in candidates]
    # Two rows may share a route when one is paused (empty harnesses). Prefer
    # the row a harness can still run.
    enabled = [key for key in matches if models[key].get("harnesses")]
    return (enabled[0] if enabled else matches[0]) if matches else None


def base_of(model_key: str | None, models: dict) -> str | None:
    if not model_key:
        return None
    return models.get(model_key, {}).get("base")


def _minutes(start, end) -> float | None:
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


_TASK_BUDGETS: dict[str, float | None] = {}


def agent_minutes(result: dict) -> float | None:
    """Wall clock the agent phase actually occupied, not the whole trial."""
    execution = result.get("agent_execution") or {}
    return _minutes(execution.get("started_at"), execution.get("finished_at"))


def agent_budget_minutes(result: dict) -> float | None:
    """Effective agent budget, read from the task's own task.toml.

    Harbor records ``override_timeout_sec: null`` and never writes the resolved
    number, so the task's declared budget times the multiplier is the only
    available source. Cached by task path, which is stable across trials.
    """
    config = result.get("config") or {}
    task_path = (config.get("task") or {}).get("path")
    if not task_path:
        return None
    if task_path not in _TASK_BUDGETS:
        budget: float | None = None
        try:
            declared = tomllib.loads((Path(task_path) / "task.toml").read_text())
            budget = declared.get("agent", {}).get("timeout_sec")
        except (OSError, ValueError):
            budget = None
        _TASK_BUDGETS[task_path] = None if budget is None else float(budget)
    budget = _TASK_BUDGETS[task_path]
    if budget is None:
        return None
    multiplier = config.get("agent_timeout_multiplier") or config.get("timeout_multiplier") or 1.0
    return budget * float(multiplier) / 60.0


def ran_over_budget(result: dict, slack_min: float = 0.25) -> bool:
    """Whether the agent phase outlasted its budget without Harbor saying so.

    Harbor raises AgentTimeoutError when ``asyncio.wait_for`` fires, but the
    cancellation can be swallowed by the agent's own process stream: one Claude
    trial ran 25 minutes on a 600 second budget and finished with no exception.
    That trial is censored by wall clock even though the flag is missing, and
    letting it into the percentiles moves a task's p90 by tens of minutes.
    """
    budget = agent_budget_minutes(result)
    spent = agent_minutes(result)
    return budget is not None and spent is not None and spent > budget + slack_min


def _reward(trial: dict) -> float | None:
    if not trial.get("reward_valid", True):
        return None
    rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
    value = rewards.get("reward")
    return None if value is None else float(value)


def load_trials(jobs_dir: Path, include_running_jobs: bool = False) -> list[Trial]:
    """Read every finished trial under a jobs directory."""
    trials: list[Trial] = []
    for job in sorted(Path(jobs_dir).iterdir()):
        summary_path = job / "result.json"
        if not summary_path.is_file():
            continue
        if not include_running_jobs and not json.loads(summary_path.read_text()).get("finished_at"):
            continue
        for trial_path in job.glob("*/result.json"):
            data = json.loads(trial_path.read_text())
            agent_info = data.get("agent_info") or {}
            config_agent = (data.get("config") or {}).get("agent") or {}
            harness = HARNESS_ALIASES.get(agent_info.get("name")) or HARNESS_ALIASES.get(config_agent.get("name"))
            model = (agent_info.get("model_info") or {}).get("name") or config_agent.get("model_name")
            trials.append(
                Trial(
                    job=job.name,
                    harness=harness,
                    model=model,
                    task=(data.get("task_name") or "").split("/")[-1],
                    reward=_reward(data),
                    timed_out=(
                        (data.get("exception_info") or {}).get("exception_type") == TIMEOUT_EXCEPTION
                        or ran_over_budget(data)
                    ),
                    duration_min=_minutes(data.get("started_at"), data.get("finished_at")),
                    finished=bool(data.get("finished_at")),
                    tokens_in=(data.get("agent_result") or {}).get("n_input_tokens"),
                    tokens_cached=(data.get("agent_result") or {}).get("n_cache_tokens"),
                    tokens_out=(data.get("agent_result") or {}).get("n_output_tokens"),
                )
            )
    return trials


def list_cost(trial: Trial, models: dict) -> float | None:
    """Price a trial from the registry, honoring promotional free windows.

    Harbor reports ``cost_usd`` from the agent's own accounting, which is the
    rate the *endpoint* charged the CLI — a devin swe-2 trial can carry a
    nonzero figure even though the route is free. The registry is the only
    honest source: ``free_until`` makes the real cost zero inside its window,
    and a model with no pricing row returns None rather than a fake number.
    """
    spec = models.get(resolve_model(trial.model, models)) or {}
    pricing = spec.get("pricing")
    if not pricing or trial.tokens_out is None:
        return None
    free_until = pricing.get("free_until")
    if free_until:
        try:
            end = datetime.fromisoformat(free_until.replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) <= end:
                return 0.0
        except ValueError:
            pass
    cached = min(trial.tokens_cached or 0, trial.tokens_in or 0)
    fresh = max(0, (trial.tokens_in or 0) - cached)
    return (
        fresh * pricing["input_per_1m"]
        + cached * pricing.get("cached_per_1m", pricing["input_per_1m"])
        + trial.tokens_out * pricing["output_per_1m"]
    ) / 1_000_000


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarise(trials: list[Trial], timeout_as_failure: bool = True) -> Cell:
    """Collapse trials for one cell into the numbers a comparison needs."""
    scored = [t for t in trials if t.reward is not None or t.timed_out]
    # A trial can time out after the work already landed, so the verifier reward
    # and the timeout are independent facts. Counting both would score one trial
    # twice, once as a pass and once as a zero.
    timed_out = [t for t in scored if t.timed_out]
    usable = [t.reward for t in scored if t.reward is not None and not t.timed_out]
    # A censored trial ran until the budget stopped it, so its duration measures
    # the clock rather than the task and would inflate the percentile.
    durations = [
        t.duration_min for t in trials if t.duration_min is not None and not t.timed_out
    ]

    if timeout_as_failure:
        values = [0.0 if t.timed_out else t.reward for t in scored if t.timed_out or t.reward is not None]
        mean_with = sum(values) / len(values) if values else None
    else:
        mean_with = sum(usable) / len(usable) if usable else None

    return Cell(
        n_usable=len(usable),
        n_timeout=len(timed_out),
        mean_without_timeouts=sum(usable) / len(usable) if usable else None,
        mean_with_timeouts=mean_with,
        p50_minutes=percentile(durations, 0.5),
        p90_minutes=percentile(durations, 0.9),
    )


def group_cells(trials: list[Trial]) -> dict[tuple, list[Trial]]:
    cells: dict[tuple, list[Trial]] = {}
    for trial in trials:
        cells.setdefault((trial.harness, trial.task), []).append(trial)
    return cells
