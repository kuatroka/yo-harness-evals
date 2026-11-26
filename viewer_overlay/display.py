"""Display names for the viewer UI.

The API carries raw identifiers: job configs name the agent class
(``harnesses.amp:AmpAgent``) and trial results name the model with its route
prefix (``engy/deepseek-v4.1-flash-1017(max)``). Neither reads well in a table,
so those two fields are rewritten at the API response boundary, which covers
every page at once.

Everything else stays raw: ``providers`` still says ``devin`` or ``engy``, and
``reasoning_effort`` keeps its own column.
"""

from __future__ import annotations

import re

EFFORT_SUFFIXES = ("xhigh", "max", "high", "medium", "low", "minimal", "none")
EFFORT_IN_NAME = re.compile(r"\((?:%s)\)$" % "|".join(EFFORT_SUFFIXES))

AGENT_DISPLAY_NAMES = {
    "harnesses.amp:AmpAgent": "Amp",
    "amp-code": "Amp",
    "amp": "Amp",
    "harnesses.droid:DroidAgent": "Droid",
    "factory-droid": "Droid",
    "droid": "Droid",
    "harnesses.claude:ClaudeCodeAgent": "Claude Code",
    "claude-code": "Claude Code",
    "claude": "Claude Code",
}


def display_agent_name(name: str | None) -> str | None:
    """Map an agent identifier to the name a person would use."""
    if not name:
        return name
    known = AGENT_DISPLAY_NAMES.get(name)
    if known:
        return known
    # Anything unregistered falls back to the class name after the module path.
    tail = name.rsplit(":", 1)[-1]
    return AGENT_DISPLAY_NAMES.get(tail, tail)


def display_model_name(model: str | None, effort: str | None = None) -> str | None:
    """Drop the route prefix and name the effort the trial ran at.

    A route either spells the effort in the model id (``devin/swe-2-max``,
    ``engy/deepseek-v4.1-flash-1017(max)``) or carries it as a separate field
    (``claude-haiku-5-5`` with ``reasoning_effort=max``). Both end up in the
    label, and a level that the id already names is never repeated.
    """
    if not model:
        return model
    name = model.split("/", 1)[1] if "/" in model else model
    for level in EFFORT_SUFFIXES:
        suffix = f"-{level}"
        if name.endswith(suffix):
            return f"{name[: -len(suffix)]}({level})"
    if effort:
        level = effort.lower()
        if level in EFFORT_SUFFIXES and not EFFORT_IN_NAME.search(name):
            return f"{name}({level})"
    return name


def display_names(names: list[str], mapper) -> list[str]:
    """Map a list of names and de-duplicate the result, keeping order."""
    return list(dict.fromkeys(mapper(name) or name for name in names))
