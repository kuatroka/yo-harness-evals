from __future__ import annotations

import json
from pathlib import Path
from typing import override

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.result import AgentInfo

from .common import provider_from_model_name


class ClaudeCodeAgent(ClaudeCode):
    """Harbor's Claude Code agent, pointed at the 9router Anthropic endpoint.

    Runs are launched with ``--skip-model-parsing-and-provider-mapping`` so the
    ``devin/...`` model IDs reach the router untouched. That flag makes Harbor
    label every trial ``harbor-skip-parsing``, so the real provider is restored
    here, matching DroidAgent and AmpAgent.
    """

    @override
    def _get_session_dir(self, logs_dir: Path | None = None) -> Path | None:
        """Pick the session this run launched, even if a nested CLI wrote one.

        The agent has a shell, so it can start a second ``claude`` process by
        accident. One trial used the Claude binary where it meant ``rg``,
        which created a second project directory under ``sessions/projects``.
        Harbor refuses to choose between two, returns None, and the whole
        trajectory is dropped along with every token total, even though the
        trial still scored 1.0. The launch's own stream log announces its
        session id in the init event, so match that file exactly first.
        """
        logs = Path(logs_dir) if logs_dir is not None else self.logs_dir
        session_id = self._launched_session_id(logs)
        if session_id is not None:
            for session_dir in self._session_dirs(logs):
                if (session_dir / f"{session_id}.jsonl").is_file():
                    return session_dir
        return super()._get_session_dir(logs_dir)

    @staticmethod
    def _launched_session_id(logs_dir: Path) -> str | None:
        """Read the session id Claude Code announced for this launch."""
        try:
            stream = (logs_dir / "claude-code.txt").open(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return None
        with stream:
            for line in stream:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    event.get("type") == "system"
                    and event.get("subtype") == "init"
                ):
                    session_id = event.get("session_id")
                    return (
                        session_id
                        if isinstance(session_id, str) and session_id
                        else None
                    )
        return None

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """Install Claude Code with bun instead of distro nodejs and npm.

        The upstream installer pulls ``nodejs`` and ``npm`` from apt, which is
        slow and drags in a full Node toolchain the native CLI does not need.
        bun installs in one script and covers any JavaScript tooling the task
        image needs.
        """
        if await self.is_requested_version_installed(environment):
            return

        await self.ensure_system_dependencies(
            environment, ("curl", "bash", "unzip", "procps")
        )
        version_flag = f" {self._version}" if self._version else ""
        path = 'export PATH="$HOME/.bun/bin:$HOME/.local/bin:$PATH"; '
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                f"{path}"
                "if ! command -v bun >/dev/null 2>&1; then "
                "  curl -fsSL https://bun.sh/install | bash; "
                "fi; "
                f"{path}"
                "bun --version; "
                "if command -v apk >/dev/null 2>&1; then "
                "  bun add -g @anthropic-ai/claude-code; "
                "else "
                "  curl -fsSL https://downloads.claude.ai/claude-code-releases/"
                f"bootstrap.sh | bash -s --{version_flag}; "
                "fi; "
                "echo 'export PATH=\"$HOME/.bun/bin:$HOME/.local/bin:$PATH\"' "
                ">> ~/.bashrc; "
                "claude --version"
            ),
        )

    @override
    def to_agent_info(self) -> AgentInfo:
        info = super().to_agent_info()
        if info.model_info and (provider := provider_from_model_name(self.model_name)):
            info.model_info.provider = provider
        return info

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        """Keep Harbor's token accounting and record the effort that produced it.

        The jobs table shows the model name alone, so a direct Anthropic run
        reads ``claude-haiku-5-5`` while the router routes read
        ``swe-2-flash(max)``. The effort is a request parameter here, so write
        it into the trial metadata where it can be checked per trial.
        """
        super().populate_context_post_run(context)
        provider = provider_from_model_name(self.model_name)
        if provider is not None and provider != "anthropic":
            # Claude Code writes its own LiteLLM-priced total_cost_usd even when
            # the model is a free or bridged devin/cx/engy route, so the trial
            # would carry a cost it never actually incurred. list_cost prices
            # those from the registry instead; clear the agent's estimate here.
            context.cost_usd = None
        context.metadata = {
            **(context.metadata or {}),
            "harness": "claude-code",
            "effort": self.options.reasoning_effort,
            "model": self.model_name,
        }
