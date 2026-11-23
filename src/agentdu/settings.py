from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str | None
    base_url: str
    api_key_env: str | None = None
    timeout: float = 120
    temperature: float = 0
    streaming: bool = True
    capabilities: tuple[str, ...] = ()
    stream_usage: bool = True
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None


@dataclass(frozen=True)
class MCPServerSettings:
    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout: float = 60


@dataclass(frozen=True)
class ModelSettings:
    profiles: dict[str, ModelProfile]
    default_profile: str | None
    mcp_servers: tuple[MCPServerSettings, ...] = ()

    @classmethod
    def load(cls, path: Path) -> "ModelSettings":
        if not path.exists():
            return cls(profiles={}, default_profile=None)
        try:
            with path.open("rb") as config_file:
                data = tomllib.load(config_file)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigurationError(f"Could not read model configuration {path}: {error}") from error
        profile_data = data.get("profiles", {})
        if not isinstance(profile_data, dict):
            raise ConfigurationError("[profiles] must contain named tables")
        profiles: dict[str, ModelProfile] = {}
        for name, values in profile_data.items():
            if not isinstance(values, dict):
                raise ConfigurationError(f"Profile '{name}' must be a TOML table")
            provider = values.get("provider", "openai-compatible")
            if provider != "openai-compatible":
                raise ConfigurationError(
                    f"Provider '{provider}' is unsupported. Profiles currently use the OpenAI-compatible API."
                )
            base_url = values.get("base_url", "https://api.openai.com/v1")
            model = values.get("model")
            api_key_env = values.get("api_key_env")
            if not isinstance(base_url, str) or not base_url.strip():
                raise ConfigurationError(f"Profile '{name}' needs a non-empty base_url")
            if model is not None and not isinstance(model, str):
                raise ConfigurationError(f"Profile '{name}' model must be a string")
            if api_key_env is not None and not isinstance(api_key_env, str):
                raise ConfigurationError(f"Profile '{name}' api_key_env must be a string")
            timeout = values.get("timeout", 120)
            temperature = values.get("temperature", 0)
            streaming = values.get("streaming", True)
            capabilities = values.get("capabilities", [])
            stream_usage = values.get("stream_usage", True)
            input_cost = values.get("input_cost_per_million")
            output_cost = values.get("output_cost_per_million")
            if (
                not isinstance(timeout, (int, float))
                or isinstance(timeout, bool)
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ConfigurationError(f"Profile '{name}' timeout must be a positive number")
            if (
                not isinstance(temperature, (int, float))
                or isinstance(temperature, bool)
                or not math.isfinite(temperature)
            ):
                raise ConfigurationError(f"Profile '{name}' temperature must be a finite number")
            if not isinstance(streaming, bool):
                raise ConfigurationError(f"Profile '{name}' streaming must be true or false")
            if not isinstance(stream_usage, bool):
                raise ConfigurationError(f"Profile '{name}' stream_usage must be true or false")
            if not isinstance(capabilities, list) or any(
                not isinstance(capability, str) or not capability.strip()
                for capability in capabilities
            ):
                raise ConfigurationError(
                    f"Profile '{name}' capabilities must be an array of non-empty strings"
                )
            for label, price in (
                ("input_cost_per_million", input_cost),
                ("output_cost_per_million", output_cost),
            ):
                if price is not None and (
                    not isinstance(price, (int, float))
                    or isinstance(price, bool)
                    or not math.isfinite(price)
                    or price < 0
                ):
                    raise ConfigurationError(
                        f"Profile '{name}' {label} must be a non-negative finite number"
                    )
            profiles[name] = ModelProfile(
                name=name,
                model=model,
                base_url=base_url,
                api_key_env=api_key_env,
                timeout=float(timeout),
                temperature=float(temperature),
                streaming=streaming,
                capabilities=tuple(capabilities),
                stream_usage=stream_usage,
                input_cost_per_million=(float(input_cost) if input_cost is not None else None),
                output_cost_per_million=(float(output_cost) if output_cost is not None else None),
            )
        default_profile = data.get("default_profile")
        if default_profile is not None and default_profile not in profiles:
            raise ConfigurationError(f"Default profile '{default_profile}' is not defined")
        mcp_servers = _parse_mcp_servers(data.get("mcp", {}))
        return cls(
            profiles=profiles,
            default_profile=default_profile,
            mcp_servers=mcp_servers,
        )

    def resolve(self, name: str | None) -> ModelProfile | None:
        if name is None:
            return None
        try:
            return self.profiles[name]
        except KeyError as error:
            available = ", ".join(sorted(self.profiles)) or "none"
            raise ConfigurationError(
                f"Unknown model profile '{name}'. Available profiles: {available}"
            ) from error


def default_config_path() -> Path:
    explicit = os.environ.get("AGENTDU_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    app_data = os.environ.get("APPDATA")
    if app_data:
        return Path(app_data) / "AgentDU" / "config.toml"
    return Path.home() / ".config" / "agentdu" / "config.toml"


def resolve_model_options(
    settings: ModelSettings,
    profile_name: str | None,
    model_override: str | None,
    base_url_override: str | None,
    api_key_override: str | None,
    streaming_override: bool | None = None,
) -> dict[str, Any]:
    selected_name = profile_name or settings.default_profile
    profile = settings.resolve(selected_name)
    model_name = model_override or (profile.model if profile else None)
    if not model_name:
        raise ConfigurationError(
            "Set --model or AGENTDU_MODEL, or define a model in the selected profile."
        )
    base_url = (
        base_url_override
        or os.environ.get("AGENTDU_BASE_URL")
        or (profile.base_url if profile else None)
        or "https://api.openai.com/v1"
    )
    api_key = api_key_override or os.environ.get("AGENTDU_API_KEY")
    if api_key is None and profile and profile.api_key_env:
        api_key = os.environ.get(profile.api_key_env)
    if api_key is None:
        api_key = os.environ.get("OPENAI_API_KEY")
    streaming = (
        streaming_override
        if streaming_override is not None
        else profile.streaming if profile else True
    )
    return {
        "model": model_name,
        "base_url": base_url,
        "api_key": api_key,
        "timeout": profile.timeout if profile else 120,
        "temperature": profile.temperature if profile else 0,
        "streaming": streaming,
        "stream_usage": profile.stream_usage if profile else True,
        "input_cost_per_million": profile.input_cost_per_million if profile else None,
        "output_cost_per_million": profile.output_cost_per_million if profile else None,
    }


def resolve_profile_options(settings: ModelSettings, profile_name: str) -> dict[str, Any]:
    profile = settings.resolve(profile_name)
    if profile is None:
        raise ConfigurationError(f"Model profile '{profile_name}' is required")
    if not profile.model:
        raise ConfigurationError(f"Model profile '{profile_name}' needs a model")

    api_key = os.environ.get(profile.api_key_env) if profile.api_key_env else None
    if api_key is None:
        api_key = os.environ.get("AGENTDU_API_KEY") or os.environ.get("OPENAI_API_KEY")
    return {
        "model": profile.model,
        "base_url": profile.base_url,
        "api_key": api_key,
        "timeout": profile.timeout,
        "temperature": profile.temperature,
        "streaming": profile.streaming,
        "stream_usage": profile.stream_usage,
        "input_cost_per_million": profile.input_cost_per_million,
        "output_cost_per_million": profile.output_cost_per_million,
    }


def _parse_mcp_servers(value: Any) -> tuple[MCPServerSettings, ...]:
    if not isinstance(value, dict):
        raise ConfigurationError("[mcp] must be a table")
    servers = value.get("servers", {})
    if not isinstance(servers, dict):
        raise ConfigurationError("[mcp.servers] must contain named tables")
    parsed: list[MCPServerSettings] = []
    for name, config in servers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
            raise ConfigurationError(f"Invalid MCP server name: {name!r}")
        if not isinstance(config, dict):
            raise ConfigurationError(f"MCP server '{name}' must be a TOML table")
        command = config.get("command")
        args = config.get("args", [])
        cwd = config.get("cwd")
        env = config.get("env", {})
        timeout = config.get("timeout", 60)
        if not isinstance(command, str) or not command.strip():
            raise ConfigurationError(f"MCP server '{name}' needs a command")
        if not isinstance(args, list) or any(not isinstance(argument, str) for argument in args):
            raise ConfigurationError(f"MCP server '{name}' args must be an array of strings")
        if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
            raise ConfigurationError(f"MCP server '{name}' cwd must be a relative directory")
        if cwd is not None and Path(cwd).is_absolute():
            raise ConfigurationError(f"MCP server '{name}' cwd must be relative to the workspace")
        if cwd is not None and ".." in Path(cwd).parts:
            raise ConfigurationError(f"MCP server '{name}' cwd must not contain parent-directory segments")
        if not isinstance(env, dict) or any(
            not isinstance(target, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target)
            or not isinstance(source, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source)
            for target, source in env.items()
        ):
            raise ConfigurationError(
                f"MCP server '{name}' env maps child variable names to environment variable names"
            )
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ConfigurationError(f"MCP server '{name}' timeout must be a positive number")
        parsed.append(
            MCPServerSettings(
                name=name,
                command=command,
                args=tuple(args),
                cwd=cwd,
                env=dict(env),
                timeout=float(timeout),
            )
        )
    return tuple(parsed)
