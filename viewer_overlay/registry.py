"""What the model registry says about models, routes and harness support.

`bench/config/models.json` is the single source. This module reads it once per
request; `bench/model_id.py` stays the shell's view of the same file. Going
through the script here would spawn one Python process per (agent, model) pair,
because the script answers one pair per invocation. `tests/test_viewer_overlay.py`
holds the two views to the same answers, so they cannot drift silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "bench" / "config" / "models.json"

AGENT_LABELS = {
    "droid": "Droid",
    "amp": "Amp",
    "codex": "Codex",
    "claude": "Claude Code",
}


@dataclass(frozen=True)
class ModelEntry:
    """One registry row, with what the launch form needs."""

    key: str
    route: str
    provider: str
    effort: str
    harnesses: tuple[str, ...]
    notes: str

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "route": self.route,
            "provider": self.provider,
            "effort": self.effort,
            "harnesses": list(self.harnesses),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class Registry:
    entries: tuple[ModelEntry, ...]

    def keys(self) -> tuple[str, ...]:
        return tuple(entry.key for entry in self.entries)

    def get(self, key: str) -> ModelEntry | None:
        for entry in self.entries:
            if entry.key == key:
                return entry
        return None

    def efforts(self) -> tuple[str, ...]:
        return tuple(sorted({entry.effort for entry in self.entries}))

    def agents(self) -> tuple[tuple[str, str], ...]:
        """Every harness any model names, with the label a person reads."""
        ids = sorted({harness for entry in self.entries for harness in entry.harnesses})
        return tuple((agent, AGENT_LABELS.get(agent, agent.title())) for agent in ids)

    def supports(self, key: str, agent: str) -> bool:
        entry = self.get(key)
        return entry is not None and agent in entry.harnesses


def load(path: Path = REGISTRY_PATH) -> Registry:
    """Read the registry file. Raises when it is missing or malformed."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = tuple(
        ModelEntry(
            key=key,
            route=spec["route"],
            provider=spec["provider"],
            effort=spec["effort"],
            harnesses=tuple(spec.get("harnesses") or ()),
            notes=spec.get("notes", ""),
        )
        for key, spec in sorted(raw.items())
    )
    return Registry(entries=entries)
