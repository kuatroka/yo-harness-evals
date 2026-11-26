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

AmpEffort = Literal["high", "xhigh", "max"]
AmpModel = Literal[
    "deepseek-flash-high",
    "deepseek-flash-max",
    "deepseek-engy-high",
    "deepseek-engy-max",
    "haiku-amp-native",
    "haiku-devin-max",
    "swe-2-max",
    "swe-2-high",
    "swe-2-flash-max",
    "swe-2-flash-high",
    "swe-2-1-xhigh",
    "swe-2-1-max",
]


class AmpOptions(InstalledAgentOptions):
    effort: AmpEffort = Field(
        default="high",
        description="Amp mode effort. The mode plugin maps this to a model variant.",
    )
    model: AmpModel | None = Field(
        default=None,
        description=(
            "Amp mode model. Defaults to the DeepSeek Flash variant for the "
            "selected effort."
        ),
    )


class AmpAgent(BaseInstalledAgent):
    """Run Amp Code inside a Harbor task container.

    Amp's model routing is account-side. The adapter writes a small local
    plugin that registers one mode per model key. Routes prefixed ``devin/`` or
    ``engy/`` resolve through the user's 9Router connection; an
    ``anthropic/...`` mode uses Amp's own Claude access and never touches
    9Router.

    ``AMP_API_KEY`` must be an Amp account access token (``sgamp_...``) from
    Settings -> Security. The short-lived session token stored by ``amp login``
    is rejected by Amp in non-interactive runs.
    """

    capabilities = AgentCapabilities(reasoning_effort=True)

    MODE_SPECS: ClassVar[dict[AmpModel, dict[str, str]]] = {
        "deepseek-flash-high": {
            "label": "DV DS Flash 4.1 high",
            "model": "devin/deepseek-v4.1-flash-high",
            "effort": "high",
            "color": "#10b981",
        },
        "deepseek-flash-max": {
            "label": "DV DS Flash 4.1 max",
            "model": "devin/deepseek-v4.1-flash-max",
            "effort": "max",
            "color": "#10b981",
        },
        "deepseek-engy-high": {
            "label": "Engy DS Flash 4.1 high",
            "model": "engy/deepseek-v4.1-flash-1017(high)",
            "effort": "high",
            "color": "#0ea5e9",
        },
        "deepseek-engy-max": {
            "label": "Engy DS Flash 4.1 max",
            "model": "engy/deepseek-v4.1-flash-1017(max)",
            "effort": "max",
            "color": "#0ea5e9",
        },
        "haiku-amp-native": {
            "label": "Amp Claude Haiku 5.5 max",
            "model": "anthropic/claude-haiku-5-5",
            "effort": "max",
            "color": "#f97316",
        },
        "haiku-devin-max": {
            "label": "DV Haiku 5.5 max",
            "model": "devin/haiku-5-5-max",
            "effort": "max",
            "color": "#f59e0b",
        },
        "swe-2-max": {
            "label": "DV SWE-2 max",
            "model": "devin/swe-2-max",
            "effort": "max",
            "color": "#7c3aed",
        },
        "swe-2-high": {
            "label": "DV SWE-2 high",
            "model": "devin/swe-2-high",
            "effort": "high",
            "color": "#7c3aed",
        },
        "swe-2-flash-max": {
            "label": "DV SWE-2 Flash max",
            "model": "devin/swe-2-flash-max",
            "effort": "max",
            "color": "#a855f7",
        },
        "swe-2-flash-high": {
            "label": "DV SWE-2 Flash high",
            "model": "devin/swe-2-flash-high",
            "effort": "high",
            "color": "#a855f7",
        },
        "swe-2-1-xhigh": {
            "label": "DV SWE-2.1 xhigh",
            "model": "devin/swe-2-1-xhigh",
            "effort": "xhigh",
            "color": "#dc2626",
        },
        "swe-2-1-max": {
            "label": "DV SWE-2.1 max",
            "model": "devin/swe-2-1-max",
            "effort": "max",
            "color": "#dc2626",
        },
    }
    DEFAULT_MODEL_BY_EFFORT: ClassVar[dict[AmpEffort, AmpModel]] = {
        "high": "deepseek-flash-high",
        "xhigh": "swe-2-1-xhigh",
        "max": "deepseek-flash-max",
    }

    PLUGIN_SOURCE = r'''
// @amp-agent-mode {"key":"fh-devin-ds-high","label":"DV DS Flash 4.1 high","color":"#10b981"}
// @amp-agent-mode {"key":"fh-devin-ds-max","label":"DV DS Flash 4.1 max","color":"#10b981"}
// @amp-agent-mode {"key":"fh-engy-ds-high","label":"Engy DS Flash 4.1 high","color":"#0ea5e9"}
// @amp-agent-mode {"key":"fh-engy-ds-max","label":"Engy DS Flash 4.1 max","color":"#0ea5e9"}
// @amp-agent-mode {"key":"fh-amp-haiku-max","label":"Amp Claude Haiku 5.5 max","color":"#f97316"}
// @amp-agent-mode {"key":"fh-devin-haiku-max","label":"DV Haiku 5.5 max","color":"#f59e0b"}
// @amp-agent-mode {"key":"fh-devin-swe2-max","label":"DV SWE-2 max","color":"#7c3aed"}
// @amp-agent-mode {"key":"fh-devin-swe2-high","label":"DV SWE-2 high","color":"#7c3aed"}
// @amp-agent-mode {"key":"fh-devin-swe2-flash-max","label":"DV SWE-2 Flash max","color":"#a855f7"}
// @amp-agent-mode {"key":"fh-devin-swe2-flash-high","label":"DV SWE-2 Flash high","color":"#a855f7"}
// @amp-agent-mode {"key":"fh-devin-swe21-xhigh","label":"DV SWE-2.1 xhigh","color":"#dc2626"}
// @amp-agent-mode {"key":"fh-devin-swe21-max","label":"DV SWE-2.1 max","color":"#dc2626"}

import type { PluginAPI } from '@ampcode/plugin'

const modes = [
  {
    key: 'fh-devin-ds-high',
    label: 'DV DS Flash 4.1 high',
    color: '#10b981',
    agent: {
      model: 'devin/deepseek-v4.1-flash-high',
      reasoningEffort: 'high',
    },
  },
  {
    key: 'fh-devin-ds-max',
    label: 'DV DS Flash 4.1 max',
    color: '#10b981',
    agent: {
      model: 'devin/deepseek-v4.1-flash-max',
      reasoningEffort: 'max',
    },
  },
  {
    key: 'fh-engy-ds-high',
    label: 'Engy DS Flash 4.1 high',
    color: '#0ea5e9',
    agent: {
      model: 'engy/deepseek-v4.1-flash-1017(high)',
      reasoningEffort: 'high',
    },
  },
  {
    key: 'fh-engy-ds-max',
    label: 'Engy DS Flash 4.1 max',
    color: '#0ea5e9',
    agent: {
      model: 'engy/deepseek-v4.1-flash-1017(max)',
      reasoningEffort: 'max',
    },
  },
  {
    key: 'fh-amp-haiku-max',
    label: 'Amp Claude Haiku 5.5 max',
    color: '#f97316',
    agent: {
      model: 'anthropic/claude-haiku-5-5',
      reasoningEffort: 'max',
    },
  },
  {
    key: 'fh-devin-haiku-max',
    label: 'DV Haiku 5.5 max',
    color: '#f59e0b',
    agent: {
      model: 'devin/haiku-5-5-max',
      reasoningEffort: 'max',
    },
  },
  {
    key: 'fh-devin-swe2-max',
    label: 'DV SWE-2 max',
    color: '#7c3aed',
    agent: {
      model: 'devin/swe-2-max',
      reasoningEffort: 'max',
    },
  },
  {
    key: 'fh-devin-swe2-high',
    label: 'DV SWE-2 high',
    color: '#7c3aed',
    agent: {
      model: 'devin/swe-2-high',
      reasoningEffort: 'high',
    },
  },
  {
    key: 'fh-devin-swe2-flash-max',
    label: 'DV SWE-2 Flash max',
    color: '#a855f7',
    agent: {
      model: 'devin/swe-2-flash-max',
      reasoningEffort: 'max',
    },
  },
  {
    key: 'fh-devin-swe2-flash-high',
    label: 'DV SWE-2 Flash high',
    color: '#a855f7',
    agent: {
      model: 'devin/swe-2-flash-high',
      reasoningEffort: 'high',
    },
  },
  {
    key: 'fh-devin-swe21-xhigh',
    label: 'DV SWE-2.1 xhigh',
    color: '#dc2626',
    agent: {
      model: 'devin/swe-2-1-xhigh',
      reasoningEffort: 'xhigh',
    },
  },
  {
    key: 'fh-devin-swe21-max',
    label: 'DV SWE-2.1 max',
    color: '#dc2626',
    agent: {
      model: 'devin/swe-2-1-max',
      reasoningEffort: 'max',
    },
  },
] as const

export default function (amp: PluginAPI) {
  for (const mode of modes) {
    const agent = amp.createAgent({
      extends: 'medium',
      model: mode.model as never,
      reasoningEffort: mode.effort,
      display: { label: mode.label, color: mode.color },
    })

    amp.registerAgentMode({
      key: mode.key,
      label: mode.label,
      description: `${mode.label} via 9Router, ${mode.effort} reasoning.`,
      color: mode.color,
      agent: agent.definition,
    })
  }
}
'''

    DEFAULT_VERSION = "latest"
    DEFAULT_SETUP_TIMEOUT_SEC = 1200.0
    SETTINGS_FILENAME = "settings.json"
    PLUGIN_FILENAME = "fh-modes.ts"

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

    def __init__(
        self,
        *args,
        effort: str | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
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
        selected_model = (
            model or self.DEFAULT_MODEL_BY_EFFORT[self.effort]
        ).lower()
        if selected_model not in self.MODE_SPECS:
            raise ValueError(
                f"Unsupported Amp model {selected_model!r}. "
                f"Choose one of: {', '.join(self.MODE_SPECS)}"
            )
        self.model: AmpModel = cast(AmpModel, selected_model)
        if self.options is not None:
            self.options.effort = self.effort
            self.options.model = self.model

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
        if str(model).lower() not in cls.MODE_SPECS:
            raise ValueError(
                f"Unsupported Amp model {model!r}. "
                f"Choose one of: {', '.join(cls.MODE_SPECS)}"
            )
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
        plugin_path = f"{home}/.config/amp/plugins/{self.PLUGIN_FILENAME}"

        await upload_text(
            self,
            environment,
            content=json.dumps({"amp.dangerouslyAllowAll": True}, indent=2),
            remote_path=settings_path,
            filename=self.SETTINGS_FILENAME,
        )
        await upload_text(
            self,
            environment,
            content=self.PLUGIN_SOURCE,
            remote_path=plugin_path,
            filename=self.PLUGIN_FILENAME,
        )
        self.logger.info(
            "Configured Amp settings and mode plugin at %s", plugin_path
        )

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
        spec = self.MODE_SPECS[self.model]
        label = spec["label"]
        log_dir = str(self.environment_logs_dir)
        log_file = f"{log_dir}/amp.txt"

        command = (
            "set -euo pipefail; "
            'export PATH="$HOME/.amp/bin:$PATH"; '
            f"mkdir -p {shlex.quote(log_dir)}; "
            f"amp --model {shlex.quote(spec['model'])} "
            f"--effort {shlex.quote(spec['effort'])} "
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
            "Running Amp Code with effort=%s model_key=%s mode=%s",
            self.effort,
            self.model,
            label,
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
        spec = self.MODE_SPECS[self.model]
        context.metadata = {
            **(context.metadata or {}),
            "harness": "amp-code",
            "effort": self.effort,
            "mode": spec["label"],
            "model": spec["model"],
            "model_key": self.model,
        }
