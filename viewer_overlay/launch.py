"""What a launch is, and what is still unresolved about it.

Pure data and pure checks. `resolve` is the only place a launch gets validated,
and it runs where the HTTP payload crosses into the domain. `resolve_from_job`
reads a finished job's own records so a rerun derives its fields instead of
asking a person to retype them. The runner trusts the spec it is handed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .registry import Registry

SPEC_FIELDS = (
    "agent",
    "model",
    "effort",
    "tasks",
    "n_concurrent",
    "attempts",
    "retries",
    "dataset",
    "from_job",
)

HARNESS_TOKENS = ("droid", "amp", "codex", "claude")

# Job configs from before the registry used full provider names spell the
# Devin route ``dv/...``. It is the same route under an older prefix, and
# without the alias those jobs load with ``model: missing`` and cannot rerun.
ROUTE_ALIASES = {"dv/": "devin/"}

DEFAULTS = {
    "n_concurrent": 1,
    "attempts": 2,
    "retries": 2,
    "dataset": "frontier-harness-eval",
}


@dataclass(frozen=True)
class LaunchSpec:
    agent: str  # droid | amp | codex | claude
    model: str  # registry key from bench/config/models.json
    effort: str
    tasks: tuple[str, ...]
    n_concurrent: int = 1
    attempts: int = 2
    retries: int = 2
    dataset: str = "frontier-harness-eval"
    from_job: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "model": self.model,
            "effort": self.effort,
            "tasks": list(self.tasks),
            "n_concurrent": self.n_concurrent,
            "attempts": self.attempts,
            "retries": self.retries,
            "dataset": self.dataset,
            "from_job": self.from_job,
        }


@dataclass(frozen=True)
class Resolution:
    """A spec when every field resolved, plus what to show when it did not.

    `spec` is None whenever anything is unresolved, so a caller never has to
    check fields one by one. `partial` carries the values that did resolve.
    """

    unresolved: tuple[str, ...] = ()
    spec: LaunchSpec | None = None
    partial: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.spec is not None and not self.unresolved


def resolve(
    candidates: Mapping[str, Any],
    registry: Registry,
    datasets_root: Path,
) -> Resolution:
    """Check a candidate field map and build the spec it describes."""
    problems: list[str] = []
    partial = partial_fields(candidates)
    agent = partial["agent"]
    model = partial["model"]
    effort = partial["effort"]
    tasks = partial["tasks"]
    dataset = partial["dataset"]

    known_agents = {agent_id for agent_id, _ in registry.agents()}
    if agent is None:
        problems.append("agent: missing")
    elif agent not in known_agents:
        problems.append(f"agent: unknown {agent!r}")

    if model is None:
        problems.append("model: missing")
    elif registry.get(model) is None:
        problems.append(f"model: unknown {model!r}")

    if effort is None:
        problems.append("effort: missing")
    elif effort not in registry.efforts():
        problems.append(f"effort: unknown {effort!r}")

    if not tasks:
        problems.append("tasks: missing")

    if (
        agent is not None
        and model is not None
        and registry.get(model) is not None
        and not registry.supports(model, agent)
    ):
        problems.append(f"model: {model!r} does not route {agent!r}")

    if tasks:
        task_root = datasets_root / dataset
        for task in tasks:
            if not (task_root / task).is_dir():
                problems.append(f"task: {task!r} is not in {dataset!r}")

    n_concurrent = _whole_number(candidates, "n_concurrent", 1, problems)
    attempts = _whole_number(candidates, "attempts", 1, problems)
    retries = _whole_number(candidates, "retries", 0, problems)

    if problems:
        return Resolution(unresolved=tuple(problems), spec=None, partial=partial)

    spec = LaunchSpec(
        agent=agent,
        model=model,
        effort=effort,
        tasks=tuple(tasks),
        n_concurrent=n_concurrent,
        attempts=attempts,
        retries=retries,
        dataset=dataset,
        from_job=partial["from_job"],
    )
    return Resolution(unresolved=(), spec=spec, partial=partial)


def resolve_from_job(
    job_dir: Path,
    registry: Registry,
    datasets_root: Path,
    job_name: str | None = None,
) -> Resolution:
    """Derive a spec from a job's own config, reporting what could not be read."""
    candidates, notes = _candidates_from_job(job_dir, registry, job_name or job_dir.name)
    resolution = resolve(candidates, registry, datasets_root)
    if not notes:
        return resolution
    merged = tuple(dict.fromkeys(notes + resolution.unresolved))
    return Resolution(unresolved=merged, spec=None, partial=resolution.partial)


def unresolved_job(job_name: str) -> Resolution:
    """A resolution for a rerun request that names a job this repo does not hold."""
    return Resolution(
        unresolved=(f"job: unknown {job_name!r}",),
        spec=None,
        partial=partial_fields({}),
    )


def _candidates_from_job(
    job_dir: Path,
    registry: Registry,
    job_name: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    config_path = job_dir / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, (f"job: cannot read {config_path.name}: {exc}",)
    if not isinstance(config, dict):
        return {}, (f"job: {config_path.name} is not an object",)

    agents = config.get("agents") or []
    first_agent = agents[0] if agents and isinstance(agents[0], dict) else {}
    kwargs = first_agent.get("kwargs") or {}
    if not isinstance(kwargs, dict):
        kwargs = {}

    agent = _harness_from_agent_name(first_agent.get("name"))
    effort = _first_text(kwargs.get("effort"), kwargs.get("reasoning_effort"))
    recorded_id = kwargs.get("model_id")
    model = (
        recorded_id
        if isinstance(recorded_id, str) and registry.get(recorded_id) is not None
        else _model_for_route(first_agent.get("model_name"), effort, registry)
    )
    if effort is None and model is not None:
        entry = registry.get(model)
        effort = entry.effort if entry is not None else None

    datasets = config.get("datasets") or []
    first_dataset = datasets[0] if datasets and isinstance(datasets[0], dict) else {}
    task_names = first_dataset.get("task_names")
    tasks = (
        tuple(name for name in task_names if isinstance(name, str))
        if isinstance(task_names, list)
        else ()
    )
    dataset = _dataset_name(first_dataset.get("path"))

    candidates: dict[str, Any] = {
        "agent": agent,
        "model": model,
        "effort": effort,
        "tasks": tasks or None,
        "dataset": dataset,
        "from_job": job_name,
    }
    return {key: value for key, value in candidates.items() if value is not None}, ()


def _harness_from_agent_name(name: Any) -> str | None:
    """Map an agent class or id to a harness by the words it contains."""
    if not isinstance(name, str) or not name:
        return None
    lowered = name.lower()
    for agent in HARNESS_TOKENS:
        if agent in lowered:
            return agent
    return None


def _model_for_route(route: Any, effort: Any, registry: Registry) -> str | None:
    """Find the registry key whose route the job recorded.

    Two rows can share a route (an Anthropic id with two effort levels), so the
    job's effort breaks the tie. A tie it cannot break stays unresolved.
    """
    if not isinstance(route, str) or not route:
        return None
    matches = [entry for entry in registry.entries if entry.route == route]
    if not matches:
        canonical = _canonical_route(route)
        matches = [entry for entry in registry.entries if entry.route == canonical]
    if len(matches) == 1:
        return matches[0].key
    if isinstance(effort, str):
        narrowed = [entry for entry in matches if entry.effort == effort]
        if len(narrowed) == 1:
            return narrowed[0].key
    return None


def _canonical_route(route: str) -> str:
    """Rewrite a route recorded under an older provider prefix."""
    for old, new in ROUTE_ALIASES.items():
        if route.startswith(old):
            return new + route.removeprefix(old)
    return route


def _dataset_name(path: Any) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    return Path(path.rstrip("/")).name or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def partial_fields(candidates: Mapping[str, Any]) -> dict[str, Any]:
    """The candidate values a spec could be built from, with holes as None."""
    partial: dict[str, Any] = {}
    for name in SPEC_FIELDS:
        value = candidates.get(name)
        if name == "tasks":
            partial[name] = (
                [task for task in value]
                if isinstance(value, (list, tuple)) and all(isinstance(task, str) for task in value)
                else None
            )
        elif name in ("n_concurrent", "attempts", "retries"):
            whole = isinstance(value, int) and not isinstance(value, bool)
            partial[name] = value if whole else None
        elif name == "from_job":
            partial[name] = value if isinstance(value, str) and value else None
        else:
            partial[name] = value if isinstance(value, str) and value else None
    if partial["dataset"] is None:
        partial["dataset"] = DEFAULTS["dataset"]
    return partial


def _whole_number(
    candidates: Mapping[str, Any],
    name: str,
    minimum: int,
    problems: list[str],
) -> int:
    value = candidates.get(name)
    if value is None:
        return int(DEFAULTS[name])
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        problems.append(f"{name}: must be a whole number >= {minimum}")
        return int(DEFAULTS[name])
    return value
