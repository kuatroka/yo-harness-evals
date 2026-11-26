from __future__ import annotations

import json
import shlex
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment

# Droid and Amp both ship glibc-linked Linux binaries. Alpine/musl task images
# will fail at install time, so fail with a clear message instead.
GLIBC_CHECK = (
    "if command -v ldd >/dev/null 2>&1 && "
    "ldd --version 2>&1 | grep -qi musl; then "
    'echo "This harness requires glibc; Alpine/musl task images are not supported." >&2; '
    "exit 1; "
    "fi"
)


async def resolve_home(
    agent: BaseInstalledAgent, environment: BaseEnvironment
) -> str:
    """Return the agent user's home directory inside the task container."""
    result = await agent.exec_as_agent(
        environment, command='printf "%s" "$HOME"', cwd="/"
    )
    home = (result.stdout or "").strip()
    if not home:
        raise RuntimeError("Could not determine the agent user's HOME directory")
    return home


async def resolve_workdir(
    agent: BaseInstalledAgent, environment: BaseEnvironment
) -> str:
    """Return the task working directory, falling back to the current directory."""
    task_env = getattr(environment, "task_env_config", None)
    workdir = getattr(task_env, "workdir", None) if task_env else None
    if workdir:
        return str(workdir)
    result = await agent.exec_as_agent(environment, command="pwd")
    return (result.stdout or "/").strip() or "/"


def require_env(agent: BaseInstalledAgent, *names: str) -> tuple[str, str]:
    """Return the first present environment variable and its name."""
    value = agent._get_env(*names)
    if value:
        return names[0], value
    joined = ", ".join(names)
    raise ValueError(
        "Missing required environment variable. Set one of: "
        f"{joined}. Pass it with --agent-env NAME=VALUE or export it in the "
        "host environment."
    )


def provider_from_model_name(model_name: str | None) -> str | None:
    """Return the routing provider prefix of a ``provider/model`` name.

    Harbor labels every trial ``harbor-skip-parsing`` because these agents set
    ``skip_model_parsing``. The prefix is the provider the request routes
    through, which is what the viewer's Provider column should show.
    """
    prefix, sep, rest = (model_name or "").partition("/")
    return prefix if sep and rest else None


async def upload_text(
    agent: BaseInstalledAgent,
    environment: BaseEnvironment,
    *,
    content: str,
    remote_path: str,
    filename: str,
) -> None:
    """Upload a private text file, creating its parent directory first."""
    parent = str(PurePosixPath(remote_path).parent)
    await agent.exec_as_agent(
        environment, command=f"mkdir -p {shlex.quote(parent)}"
    )
    await agent._upload_config_text(
        environment,
        content=content,
        remote_path=remote_path,
        filename=filename,
    )


def _as_int(value: object) -> int:
    """Coerce a possibly-null usage field to an int."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def normalize_usage(usage: dict[str, object]) -> tuple[int, int, int]:
    """Return ``(input including cache, output, cache)`` from an agent usage payload.

    Anthropic-style payloads report ``input_tokens`` without the cached reads
    and writes, while Harbor's ``AgentContext.n_input_tokens`` is documented to
    include cache. Harbor's Claude Code adapter uses the same normalization.
    """
    cache_read = _as_int(
        usage.get("cache_read_input_tokens")
        or usage.get("cached_input_tokens")
        or usage.get("cached_tokens")
    )
    cache_creation = _as_int(usage.get("cache_creation_input_tokens"))
    cache_tokens = cache_read + cache_creation

    if "input_tokens" in usage:
        input_tokens = _as_int(usage.get("input_tokens")) + cache_tokens
    else:
        # OpenAI-style ``prompt_tokens`` already includes cached tokens.
        input_tokens = _as_int(usage.get("prompt_tokens"))

    output_tokens = _as_int(
        usage.get("output_tokens") or usage.get("completion_tokens")
    )
    return input_tokens, output_tokens, cache_tokens


def _parse_json_object(line: str) -> dict[str, object] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        start = line.find("{")
        end = line.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(line[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def iter_json_events(path: Path) -> Iterator[dict[str, object]]:
    """Yield JSON objects from a JSONL log, skipping non-JSON lines.

    Agent output is teed together with stderr, so a log can contain warnings
    before, after, or between JSON events.
    """
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            event = _parse_json_object(line)
            if event is not None:
                yield event


def droid_usage_totals(path: Path) -> tuple[int, int, int] | None:
    """Read the last Droid ``result`` event's usage totals."""
    usage: dict[str, object] | None = None
    for event in iter_json_events(path):
        candidate = event.get("usage")
        if event.get("type") == "result" and isinstance(candidate, dict):
            usage = candidate
    return normalize_usage(usage) if usage is not None else None


def amp_usage_totals(path: Path) -> tuple[int, int, int] | None:
    """Sum per-turn usage from Amp's stream-json assistant events."""
    totals = [0, 0, 0]
    found = False
    seen_message_ids: set[object] = set()
    for event in iter_json_events(path):
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        message_id = message.get("id")
        if message_id is not None:
            if message_id in seen_message_ids:
                continue
            seen_message_ids.add(message_id)
        input_tokens, output_tokens, cache_tokens = normalize_usage(usage)
        totals[0] += input_tokens
        totals[1] += output_tokens
        totals[2] += cache_tokens
        found = True
    if not found:
        return None
    return totals[0], totals[1], totals[2]
