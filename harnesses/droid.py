from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import ClassVar, Literal, cast, override

from pydantic import Field

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import (
    ApiUsageLimitError,
    BaseInstalledAgent,
    ErrorPattern,
    with_prompt_template,
)
from harbor.agents.options import InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.result import AgentInfo

from .common import (
    GLIBC_CHECK,
    droid_usage_totals,
    provider_from_model_name,
    require_env,
    resolve_home,
    resolve_workdir,
    upload_text,
)

DroidEffort = Literal["high", "xhigh", "max"]


class DroidOptions(InstalledAgentOptions):
    effort: DroidEffort = Field(
        default="high",
        description="Droid custom-model effort. The model ID encodes this value.",
    )
    model: str | None = Field(
        default=None,
        description=(
            "Route for the Droid custom model, for example devin/swe-2-1-max. "
            "Defaults to the DeepSeek Flash variant for the selected effort."
        ),
    )
    model_id: str | None = Field(
        default=None,
        description="Canonical registry id, for the settings label and metadata.",
    )
    model_base_url: str | None = Field(
        default=None,
        description="Base URL for the Droid custom-model endpoint.",
    )


class DroidAgent(BaseInstalledAgent):
    """Run Factory Droid inside a Harbor task container.

    Droid custom models encode reasoning effort in the model ID. The adapter
    writes a minimal Factory settings file at configure() time and invokes
    ``droid exec`` for each trial. The registry owns model names and routes;
    this adapter receives a route and keeps no second model table.
    """

    capabilities = AgentCapabilities(reasoning_effort=True)

    DEFAULT_MODEL_BY_EFFORT: ClassVar[dict[DroidEffort, str]] = {
        "high": "devin/deepseek-v4.1-flash-high",
        "xhigh": "devin/swe-2-1-xhigh",
        "max": "devin/deepseek-v4.1-flash-max",
    }
    DEFAULT_BASE_URL = "http://host.docker.internal:20128/v1"
    DEFAULT_VERSION = "1.15.0"
    DEFAULT_SETUP_TIMEOUT_SEC = 1200.0
    NODE_VERSION = "22.23.5"
    SETTINGS_FILENAME = "harbor-settings.json"
    HOST_AUTH_DIR_ENV = "DROID_HOST_AUTH_DIR"

    # A clean container has no browser login. If FACTORY_API_KEY is absent, copy
    # the host CLI's disk-backed v2 login instead. The keychain variant is also
    # accepted because the same encrypted login works when copied to Linux.
    HOST_AUTH_FILE_SETS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("auth.v2.file", "auth.v2.key"),
        ("auth.v2.loginkeychain", "auth.v2.key"),
    )

    ERROR_PATTERNS = [
        *BaseInstalledAgent.ERROR_PATTERNS,
        # Devin refuses the Haiku route with "weekly usage quota has been
        # exhausted". Droid exits non-zero with that text, so classify it as a
        # usage limit instead of accepting the trial as a zero.
        ErrorPattern(
            r"failed_precondition|weekly usage quota has been exhausted",
            ApiUsageLimitError,
        ),
    ]

    options_model = DroidOptions

    @staticmethod
    @override
    def name() -> str:
        return "factory-droid"

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
                f"Droid model must be a route like devin/swe-2-1-max, got {value!r}"
            )
        return route

    def __init__(
        self,
        *args,
        effort: str | None = None,
        reasoning_effort: str | None = None,
        model: str | None = None,
        model_id: str | None = None,
        model_base_url: str | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        selected = (effort or reasoning_effort or "high").lower()
        if selected not in self.DEFAULT_MODEL_BY_EFFORT:
            raise ValueError(
                f"Unsupported Droid effort {selected!r}. "
                f"Choose one of: {', '.join(self.DEFAULT_MODEL_BY_EFFORT)}"
            )
        self.effort: DroidEffort = cast(DroidEffort, selected)
        self.model: str = self._route(
            model or self.DEFAULT_MODEL_BY_EFFORT[self.effort]
        )
        self.model_id: str = model_id or self.model
        self.model_base_url = (
            model_base_url
            or self._get_env("DROID_MODEL_BASE_URL")
            or self.DEFAULT_BASE_URL
        ).rstrip("/")
        if self.options is not None:
            self.options.effort = self.effort
            self.options.model = self.model
            self.options.model_id = self.model_id
            self.options.model_base_url = self.model_base_url
        self._settings_path: str | None = None
        self._auth_mode: str | None = None
        self._factory_key: str | None = None

    @override
    def version(self) -> str:
        return self._version or self.DEFAULT_VERSION

    @classmethod
    def _host_auth_dir(cls, env: dict[str, str] | None = None) -> Path:
        value = (
            (env or {}).get(cls.HOST_AUTH_DIR_ENV)
            or os.environ.get(cls.HOST_AUTH_DIR_ENV)
            or "~/.factory"
        )
        return Path(value).expanduser()

    @classmethod
    def _read_host_auth_files(
        cls, base_dir: str | Path | None = None
    ) -> list[tuple[str, str]]:
        base = Path(base_dir or "~/.factory").expanduser()
        for names in cls.HOST_AUTH_FILE_SETS:
            paths = [base / name for name in names]
            if all(path.is_file() for path in paths):
                try:
                    return [
                        (name, path.read_text(encoding="utf-8"))
                        for name, path in zip(names, paths)
                    ]
                except OSError as exc:
                    raise ValueError(
                        f"Could not read Droid host auth file under {base}: {exc}"
                    ) from exc
        return []

    @classmethod
    def _host_auth_error(cls, base_dir: Path) -> str:
        return (
            "Droid is not authenticated. Set FACTORY_API_KEY, or log in to "
            f"Droid on the host so that {base_dir}/auth.v2.file and "
            f"{base_dir}/auth.v2.key exist (or "
            f"{base_dir}/auth.v2.loginkeychain and {base_dir}/auth.v2.key). "
            "The adapter copies those files into the task container. Re-run "
            "droid on the host if the login has expired."
        )

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
                f"Unsupported Droid effort {effort!r}. "
                f"Choose one of: {', '.join(cls.DEFAULT_MODEL_BY_EFFORT)}"
            )
        model = (kwargs or {}).get("model") or cls.DEFAULT_MODEL_BY_EFFORT[
            cast(DroidEffort, effort_key)
        ]
        cls._route(str(model))
        if not (
            values.get("NINEROUTER_API_KEY")
            or values.get("DROID_MODEL_API_KEY")
            or os.environ.get("NINEROUTER_API_KEY")
            or os.environ.get("DROID_MODEL_API_KEY")
        ):
            raise ValueError(
                "NINEROUTER_API_KEY (or DROID_MODEL_API_KEY) is required for "
                "Droid custom models. Pass it with --agent-env or export it "
                "in the host environment."
            )
        if not (values.get("FACTORY_API_KEY") or os.environ.get("FACTORY_API_KEY")):
            base_dir = cls._host_auth_dir(values)
            if not cls._read_host_auth_files(base_dir):
                raise ValueError(cls._host_auth_error(base_dir))

    @override
    def get_version_command(self) -> str | None:
        return "droid --version"

    @override
    def parse_version(self, stdout: str) -> str:
        text = stdout.strip()
        return text.splitlines()[0] if text else self.DEFAULT_VERSION

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(
            environment,
            ("curl", "bash", "git", "tar", "xz", "ca_certificates"),
        )

        install_node = f"""
set -euo pipefail
{GLIBC_CHECK}
if command -v node >/dev/null 2>&1; then
  major="$(node --version | sed 's/^v//' | cut -d. -f1)"
  if [ -n "${{major}}" ] && [ "${{major}}" -ge 20 ]; then
    echo "Node $(node --version) already satisfies the requirement"
    exit 0
  fi
fi
case "$(uname -m)" in
  x86_64|amd64) node_arch=x64 ;;
  aarch64|arm64) node_arch=arm64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac
node_url="https://nodejs.org/dist/v{self.NODE_VERSION}/node-v{self.NODE_VERSION}-linux-${{node_arch}}.tar.xz"
curl -fsSL "$node_url" -o /tmp/node.tar.xz
tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1
rm -f /tmp/node.tar.xz
node --version
npm --version
"""
        await self.exec_as_root(environment, command=install_node)
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                "export DROID_DISABLE_AUTO_UPDATE=true; "
                "export FACTORY_DROID_AUTO_UPDATE_ENABLED=false; "
                f"npm install -g droid@{self.DEFAULT_VERSION}; "
                "droid --version"
            ),
            env={"npm_config_update_notifier": "false"},
        )
        self.logger.info("Installed Factory Droid %s", self.DEFAULT_VERSION)

    @override
    async def configure(self, environment: BaseEnvironment) -> None:
        key_name, api_key = require_env(
            self, "NINEROUTER_API_KEY", "DROID_MODEL_API_KEY"
        )
        self._model_api_key_name = key_name
        self._factory_key = self._get_env("FACTORY_API_KEY")

        home = await resolve_home(self, environment)
        self._settings_path = f"{home}/.factory/{self.SETTINGS_FILENAME}"

        host_auth: list[tuple[str, str]] = []
        if self._factory_key:
            self._auth_mode = "api_key"
        else:
            auth_dir = self._get_env(self.HOST_AUTH_DIR_ENV)
            base_dir = self._host_auth_dir(
                {self.HOST_AUTH_DIR_ENV: auth_dir} if auth_dir else None
            )
            host_auth = self._read_host_auth_files(base_dir)
            if not host_auth:
                raise ValueError(self._host_auth_error(base_dir))
            self._auth_mode = "host_auth"
            for name, content in host_auth:
                await upload_text(
                    self,
                    environment,
                    content=content,
                    remote_path=f"{home}/.factory/{name}",
                    filename=name,
                )
            self.logger.info(
                "Copied Droid host login files into the container: %s",
                ", ".join(name for name, _ in host_auth),
            )

        model_id = f"custom:{self.model}"
        custom_models: list[dict[str, object]] = [
            {
                "id": model_id,
                "model": self.model,
                "index": 0,
                "baseUrl": self.model_base_url,
                "apiKey": api_key,
                "provider": "generic-chat-completion-api",
                "displayName": self.model_id,
                "maxOutputTokens": 128000,
                "noImageSupport": False,
            }
        ]

        await upload_text(
            self,
            environment,
            content=json.dumps({"customModels": custom_models}, indent=2),
            remote_path=self._settings_path,
            filename="settings.json",
        )
        self.logger.info(
            "Configured Droid settings at %s (base URL %s)",
            self._settings_path,
            self.model_base_url,
        )
        if "127.0.0.1" in self.model_base_url or "localhost" in self.model_base_url:
            self.logger.warning(
                "DROID_MODEL_BASE_URL points at localhost. Inside a task "
                "container use host.docker.internal or host.containers.internal."
            )

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if self._settings_path is None or self._auth_mode is None:
            await self.configure(environment)
        assert self._settings_path is not None

        workdir = await resolve_workdir(self, environment)
        model_id = f"custom:{self.model}"
        env: dict[str, str] = {}
        factory_key = (
            self._factory_key
            if self._factory_key is not None
            else self._get_env("FACTORY_API_KEY")
        )
        if factory_key:
            env["FACTORY_API_KEY"] = factory_key
        elif self._auth_mode != "host_auth":
            raise RuntimeError("Droid authentication was not configured")

        log_dir = str(self.environment_logs_dir)
        log_file = f"{log_dir}/droid.txt"
        command = (
            "set -euo pipefail; "
            "export DROID_DISABLE_AUTO_UPDATE=true; "
            "export FACTORY_DROID_AUTO_UPDATE_ENABLED=false; "
            f"mkdir -p {shlex.quote(log_dir)}; "
            f"droid exec --settings {shlex.quote(self._settings_path)} "
            f"--model {shlex.quote(model_id)} "
            "--skip-permissions-unsafe "
            f"--cwd {shlex.quote(workdir)} "
            f"--output-format json {shlex.quote(instruction)} "
            f"2>&1 | tee {shlex.quote(log_file)}"
        )
        self.logger.info(
            "Running Factory Droid with effort=%s route=%s",
            self.effort,
            self.model,
        )
        await self.exec_as_agent(environment, command=command, env=env, cwd=workdir)

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        usage = droid_usage_totals(self.logs_dir / "droid.txt")
        if usage is not None:
            context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens = (
                usage
            )
        context.metadata = {
            **(context.metadata or {}),
            "harness": "factory-droid",
            "effort": self.effort,
            "model": f"custom:{self.model}",
            "model_key": self.model,
            "model_id": self.model_id,
            "auth": self._auth_mode or "unknown",
        }
