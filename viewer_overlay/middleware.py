"""Rewrite the viewer's own JSON on the way out.

Harbor's API carries raw identifiers, so the labels a person reads are fixed at
the response boundary here instead of inside the compiled SPA. That keeps the
mapping in one place, survives a bundle rebuild, and lets the jobs table show a
total token column that the installed bundle does not know about.

Buffering is safe because these are API payloads, not streams. Anything that is
not JSON, and any `/stream` path, is replayed byte for byte.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from .display import display_agent_name, display_model_name, display_names

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

AGENT_KEYS = frozenset({"agent_name", "reported_agent_name"})
JSON_TYPE = b"application/json"


class OverlayJSONMiddleware:
    """Pure ASGI middleware over the viewer's JSON API responses."""

    def __init__(self, app: Any, jobs_dir: Path | None = None) -> None:
        self.app = app
        self.jobs_dir = (
            Path(jobs_dir).expanduser().resolve() if jobs_dir is not None else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not eligible(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        # A response is only buffered once its start message says it is JSON.
        # Everything else, recordings and images included, streams straight
        # through without ever being held in memory.
        state: dict[str, Any] = {"start": None, "buffering": None, "bodies": []}

        async def inspect(message: Message) -> None:
            kind = message.get("type")
            if state["buffering"] is False:
                await send(message)
                return
            if kind == "http.response.start":
                state["start"] = message
                state["buffering"] = _is_json(list(message.get("headers") or []))
                if not state["buffering"]:
                    await send(message)
                return
            if kind == "http.response.body":
                if not state["buffering"]:
                    await send(message)
                    return
                state["bodies"].append(message)
                if message.get("more_body", False):
                    return
                await _flush(state, send, self.jobs_dir)
                return
            await send(message)

        await self.app(scope, receive, inspect)


def eligible(path: str) -> bool:
    """Only the buffered JSON API is in scope."""
    return path.startswith("/api/") and "/stream" not in path


def rewrite_json(value: Any, jobs_dir: Path | None = None) -> Any:
    """Map every label in a payload, and total the token pairs it carries.

    A job whose top-level ``config.json`` is missing still has the same facts in
    its trial configs. When the viewer is given the jobs folder, an empty job
    summary is filled from those trials before the labels are mapped, so the
    table never shows a job with blank Source, Agents, or Models.
    """
    if isinstance(value, dict):
        return _rewrite_mapping(value, jobs_dir)
    if isinstance(value, list):
        return [rewrite_json(item, jobs_dir=jobs_dir) for item in value]
    return value


def _rewrite_mapping(node: dict[str, Any], jobs_dir: Path | None) -> dict[str, Any]:
    if jobs_dir is not None:
        node = _fill_job_metadata(node, jobs_dir)

    effort = node.get("reasoning_effort")
    effort = effort if isinstance(effort, str) else None
    all_tokens = _sum_of(node, "total_input_tokens", "total_output_tokens")
    turn_tokens = _sum_of(node, "input_tokens", "output_tokens")

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in AGENT_KEYS and isinstance(value, str):
            out[key] = display_agent_name(value)
        elif key == "agents" and _all_text(value):
            out[key] = display_names(list(value), display_agent_name)
        elif key == "models" and _all_text(value):
            out[key] = display_names(
                list(value), lambda name: display_model_name(name, effort)
            )
        elif key == "model_name" and isinstance(value, str):
            out[key] = display_model_name(value, effort)
        else:
            out[key] = rewrite_json(value, jobs_dir=jobs_dir)

        if key == "total_output_tokens" and all_tokens is not None:
            if "total_all_tokens" not in node:
                out["total_all_tokens"] = all_tokens
        if key == "output_tokens" and turn_tokens is not None:
            if "total_tokens" not in node:
                out["total_tokens"] = turn_tokens
    return out


def _fill_job_metadata(node: dict[str, Any], jobs_dir: Path) -> dict[str, Any]:
    """Fill a job summary's missing columns from the job's trial configs."""
    if not _empty_job_summary(node):
        return node
    name = node.get("name")
    if not isinstance(name, str) or not name:
        return node
    metadata = _trial_metadata(jobs_dir, name)
    if not metadata:
        return node
    filled = dict(node)
    for key, value in metadata.items():
        if key == "environment_type":
            if filled.get(key) is None:
                filled[key] = value
        elif not filled.get(key):
            filled[key] = value
    return filled


def _empty_job_summary(node: dict[str, Any]) -> bool:
    return (
        isinstance(node.get("name"), str)
        and isinstance(node.get("datasets"), list)
        and isinstance(node.get("agents"), list)
        and isinstance(node.get("models"), list)
        and any(not node.get(key) for key in ("datasets", "agents", "models"))
    )


def _trial_metadata(jobs_dir: Path, job_name: str) -> dict[str, Any]:
    """The same summary fields Harbor reads from a job config, from trials."""
    job_dir = _job_dir(jobs_dir, job_name)
    if job_dir is None:
        return {}

    datasets: set[str] = set()
    agents: set[str] = set()
    providers: set[str] = set()
    models: set[str] = set()
    environments: set[str] = set()

    for path in sorted(job_dir.glob("*/config.json")):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(config, dict):
            continue

        task = config.get("task")
        if isinstance(task, dict):
            source = task.get("source")
            if isinstance(source, str) and source:
                datasets.add(source)
            else:
                task_path = task.get("path")
                if isinstance(task_path, str) and task_path:
                    datasets.add(Path(task_path).name)

        agent = config.get("agent")
        if not isinstance(agent, dict):
            continue
        agent_name = agent.get("name")
        if isinstance(agent_name, str) and agent_name:
            agents.add(agent_name)
        model_name = agent.get("model_name")
        if isinstance(model_name, str) and model_name:
            provider, separator, model = model_name.partition("/")
            if separator:
                providers.add(provider)
                models.add(model)
            else:
                models.add(model_name)

        environment = config.get("environment")
        if isinstance(environment, dict):
            environment_type = environment.get("type")
            if isinstance(environment_type, str) and environment_type:
                environments.add(environment_type)

    return {
        "datasets": sorted(datasets),
        "agents": sorted(agents),
        "providers": sorted(providers),
        "models": sorted(models),
        "environment_type": (
            next(iter(environments)) if len(environments) == 1 else None
        ),
    }


def _job_dir(jobs_dir: Path, job_name: str) -> Path | None:
    candidate = (jobs_dir / job_name).resolve()
    try:
        candidate.relative_to(jobs_dir)
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


def _all_text(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _sum_of(node: dict[str, Any], first: str, second: str) -> int | None:
    left = node.get(first)
    right = node.get(second)
    if _is_whole(left) and _is_whole(right):
        return left + right
    return None


def _is_whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def _flush(
    state: dict[str, Any], send: Send, jobs_dir: Path | None
) -> None:
    start = state["start"]
    bodies = state["bodies"]
    headers = list(start.get("headers") or [])
    body = b"".join(message.get("body", b"") for message in bodies)
    rewritten = _rewritten_body(headers, body, jobs_dir)
    if rewritten is None:
        await send(start)
        for message in bodies:
            await send(message)
        return

    await send({**start, "headers": _with_content_length(headers, len(rewritten))})
    await send({"type": "http.response.body", "body": rewritten})


def _rewritten_body(
    headers: list[tuple[bytes, bytes]], body: bytes, jobs_dir: Path | None
) -> bytes | None:
    if not _is_json(headers):
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    rewritten = rewrite_json(payload, jobs_dir=jobs_dir)
    if rewritten == payload:
        return None
    return json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _is_json(headers: list[tuple[bytes, bytes]]) -> bool:
    for name, value in headers:
        if name.lower() == b"content-type":
            return JSON_TYPE in value.lower()
    return False


def _with_content_length(
    headers: list[tuple[bytes, bytes]], length: int
) -> list[tuple[bytes, bytes]]:
    value = str(length).encode("ascii")
    out: list[tuple[bytes, bytes]] = []
    replaced = False
    for name, current in headers:
        if name.lower() == b"content-length":
            out.append((name, value))
            replaced = True
        else:
            out.append((name, current))
    if not replaced:
        out.append((b"content-length", value))
    return out
