from __future__ import annotations

import json
import os
import shlex
from typing import ClassVar, Literal, cast, override

from pydantic import Field

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import (
    AgentAuthenticationError,
    ApiInternalServerError,
    ApiUsageLimitError,
    BaseInstalledAgent,
    ErrorPattern,
    NetworkConnectionError,
    with_prompt_template,
)
from harbor.agents.options import InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.result import AgentInfo

from .common import (
    GLIBC_CHECK,
    amp_usage_totals,
    provider_from_model_name,
    require_env,
    resolve_home,
    resolve_workdir,
    upload_text,
)

AmpEffort = Literal["medium", "high", "xhigh", "max"]


class AmpOptions(InstalledAgentOptions):
    effort: AmpEffort = Field(
        default="high",
        description="Amp reasoning effort, passed to the CLI with --effort.",
    )
    model: str | None = Field(
        default=None,
        description=(
            "Route for Amp's model selector, for example devin/swe-2-1-max. "
            "Defaults to the DeepSeek Flash variant for the selected effort."
        ),
    )
    model_id: str | None = Field(
        default=None,
        description="Canonical registry id, for metadata.",
    )


class AmpAgent(BaseInstalledAgent):
    """Run Amp Code inside a Harbor task container.

    Amp takes the route with ``--model`` and the level with ``--effort``. The
    registry owns model names and routes; this adapter keeps no mode table and
    uploads no plugin.

    Routes prefixed ``devin/`` or ``engy/`` resolve through the user's 9Router
    connection; an ``anthropic/...`` route uses Amp's own Claude access and
    never touches 9Router.

    ``AMP_API_KEY`` must be an Amp account access token (``sgamp_...``) from
    Settings -> Security. The short-lived session token stored by ``amp login``
    is rejected by Amp in non-interactive runs.
    """

    capabilities = AgentCapabilities(reasoning_effort=True)

    DEFAULT_MODEL_BY_EFFORT: ClassVar[dict[AmpEffort, str]] = {
        "medium": "devin/swe-2-1-medium",
        "high": "devin/deepseek-v4.1-flash-high",
        "xhigh": "devin/swe-2-1-xhigh",
        "max": "devin/deepseek-v4.1-flash-max",
    }
    DEFAULT_VERSION = "latest"
    DEFAULT_SETUP_TIMEOUT_SEC = 1200.0
    SETTINGS_FILENAME = "settings.json"

    ERROR_PATTERNS = [
        *BaseInstalledAgent.ERROR_PATTERNS,
        ErrorPattern(
            r"Invalid or missing API key", AgentAuthenticationError
        ),
        # Amp can exit 0 after printing a gateway error. The run command below
        # also turns that case into a non-zero exit so Harbor records it as an
        # infrastructure error rather than a failed task attempt.
        ErrorPattern(
            r"(?:^|\n)\s*Error: 502(?:\s|$)", ApiInternalServerError
        ),
        # Devin answers Haiku with a 503 that wraps a 502 quota refusal. Amp
        # prints it in a result event and exits 0, so the run command below
        # turns it non-zero and this pattern keeps it out of the score.
        ErrorPattern(
            r"failed_precondition|weekly usage quota has been exhausted",
            ApiUsageLimitError,
        ),
        ErrorPattern(
            r"(?:^|\n)\s*Error: Connection error\.?$",
            NetworkConnectionError,
        ),
    ]

    options_model = AmpOptions

    @staticmethod
    @override
    def name() -> str:
        return "amp-code"

    @override
    def to_agent_info(self) -> AgentInfo:
        info = super().to_agent_info()
        if info.model_info and (provider := provider_from_model_name(self.model_name)):
            info.model_info.provider = provider
        return info

    @staticmethod
    def _route(value: str) -> str:
        """Reject an old-style key or an already-prefixed custom id."""
        route = value.lower()
        if "/" not in route or route.startswith("custom:"):
            raise ValueError(
                f"Amp model must be a route like devin/swe-2-1-max, got {value!r}"
            )
        return route

    def __init__(
        self,
        *args,
        effort: str | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
        model_id: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        selected = (effort or reasoning_effort or "high").lower()
        if selected not in self.DEFAULT_MODEL_BY_EFFORT:
            raise ValueError(
                f"Unsupported Amp effort {selected!r}. "
                f"Choose one of: {', '.join(self.DEFAULT_MODEL_BY_EFFORT)}"
            )
        self.effort: AmpEffort = cast(AmpEffort, selected)
        self.model: str = self._route(
            model or self.DEFAULT_MODEL_BY_EFFORT[self.effort]
        )
        self.model_id: str = model_id or self.model
        if self.options is not None:
            self.options.effort = self.effort
            self.options.model = self.model
            self.options.model_id = self.model_id

    @override
    def version(self) -> str:
        return self._version or self.DEFAULT_VERSION

    @classmethod
    @override
    def preflight(cls, kwargs=None, env=None) -> None:
        super().preflight(kwargs, env)
        values = dict(env or {})
        effort = (
            (kwargs or {}).get("effort")
            or (kwargs or {}).get("reasoning_effort")
            or "high"
        )
        effort_key = str(effort).lower()
        if effort_key not in cls.DEFAULT_MODEL_BY_EFFORT:
            raise ValueError(
                f"Unsupported Amp effort {effort!r}. "
                f"Choose one of: {', '.join(cls.DEFAULT_MODEL_BY_EFFORT)}"
            )
        model = (kwargs or {}).get("model") or cls.DEFAULT_MODEL_BY_EFFORT[
            cast(AmpEffort, effort_key)
        ]
        cls._route(str(model))
        if not (values.get("AMP_API_KEY") or os.environ.get("AMP_API_KEY")):
            raise ValueError(
                "AMP_API_KEY is required for Amp headless execution. Mint an "
                "Amp access token and pass it with --agent-env or export it "
                "in the host environment."
            )

    @override
    def get_version_command(self) -> str | None:
        return 'export PATH="$HOME/.amp/bin:$PATH"; amp version'

    @override
    def parse_version(self, stdout: str) -> str:
        text = stdout.strip()
        return text.splitlines()[0] if text else self.DEFAULT_VERSION

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(
            environment,
            ("curl", "bash", "tar", "ca_certificates"),
        )
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                f"{GLIBC_CHECK}; "
                "curl -fsSL https://ampcode.com/install.sh | bash; "
                'export PATH="$HOME/.amp/bin:$PATH"; '
                "amp version"
            ),
        )
        self.logger.info("Installed Amp CLI")

    @override
    async def configure(self, environment: BaseEnvironment) -> None:
        key_name, _ = require_env(self, "AMP_API_KEY")
        self._model_api_key_name = key_name

        home = await resolve_home(self, environment)
        settings_path = f"{home}/.config/amp/{self.SETTINGS_FILENAME}"

        await upload_text(
            self,
            environment,
            content=json.dumps({"amp.dangerouslyAllowAll": True}, indent=2),
            remote_path=settings_path,
            filename=self.SETTINGS_FILENAME,
        )
        self.logger.info("Configured Amp settings at %s", settings_path)

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        _, api_key = require_env(self, "AMP_API_KEY")
        workdir = await resolve_workdir(self, environment)
        log_dir = str(self.environment_logs_dir)
        log_file = f"{log_dir}/amp.txt"

        command = (
            "set -euo pipefail; "
            'export PATH="$HOME/.amp/bin:$PATH"; '
            f"mkdir -p {shlex.quote(log_dir)}; "
            f"amp --model {shlex.quote(self.model)} "
            f"--effort {shlex.quote(self.effort)} "
            "--stream-json "
            f"-x {shlex.quote(instruction)} "
            f"2>&1 | tee {shlex.quote(log_file)}; "
            f"if grep -Eq '^[[:space:]]*Error: 502([[:space:]]|$)' {shlex.quote(log_file)} || "
            f"grep -Eq '\"type\":\"result\".*\"is_error\":true.*(Error: 502|502 Bad Gateway)' {shlex.quote(log_file)}; "
            "then echo 'Error: 502 Bad Gateway' >&2; exit 1; fi; "
            f"if grep -Eq '\"type\":\"result\".*\"is_error\":true.*(failed_precondition|weekly usage quota)' {shlex.quote(log_file)}; "
            "then echo 'Error: provider quota or precondition failure' >&2; exit 1; fi; "
            f"if grep -Eq '^[[:space:]]*Error: Connection error\\.?$' {shlex.quote(log_file)} || "
            f"grep -Eq '\"type\":\"result\".*\"is_error\":true.*Connection error' {shlex.quote(log_file)}; "
            "then echo 'Error: Connection error.' >&2; exit 1; fi"
        )
        self.logger.info(
            "Running Amp Code with effort=%s route=%s",
            self.effort,
            self.model,
        )
        await self.exec_as_agent(
            environment,
            command=command,
            env={"AMP_API_KEY": api_key, "NO_COLOR": "1"},
            cwd=workdir,
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        usage = amp_usage_totals(self.logs_dir / "amp.txt")
        if usage is not None:
            context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens = (
                usage
            )
        context.metadata = {
            **(context.metadata or {}),
            "harness": "amp-code",
            "effort": self.effort,
            "model": self.model,
            "model_key": self.model,
            "model_id": self.model_id,
        }
