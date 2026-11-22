from __future__ import annotations

import os
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


@dataclass(frozen=True)
class ModelSettings:
    profiles: dict[str, ModelProfile]
    default_profile: str | None

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
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ConfigurationError(f"Profile '{name}' timeout must be a positive number")
            if not isinstance(temperature, (int, float)):
                raise ConfigurationError(f"Profile '{name}' temperature must be numeric")
            if not isinstance(streaming, bool):
                raise ConfigurationError(f"Profile '{name}' streaming must be true or false")
            profiles[name] = ModelProfile(
                name=name,
                model=model,
                base_url=base_url,
                api_key_env=api_key_env,
                timeout=float(timeout),
                temperature=float(temperature),
                streaming=streaming,
            )
        default_profile = data.get("default_profile")
        if default_profile is not None and default_profile not in profiles:
            raise ConfigurationError(f"Default profile '{default_profile}' is not defined")
        return cls(profiles=profiles, default_profile=default_profile)

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
    }
