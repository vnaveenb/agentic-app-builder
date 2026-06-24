"""Model/provider configuration — loads and validates config/models.yaml.

This module is the single point of truth for *which* providers and models exist
and *how* each agent role is tuned. See config/models.yaml for the data itself.

The parsed config is cached after first load. Set MODELS_CONFIG_PATH to point at
a different file (used in tests).
"""

from __future__ import annotations

import functools
import os
import pathlib

import yaml
from pydantic import BaseModel, Field

# Repo root = three parents up from this file (src/dev_agent/config.py).
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "models.yaml"


class ModelSpec(BaseModel):
    id: str
    label: str = ""
    default: bool = False


class ProviderSpec(BaseModel):
    id: str
    label: str = ""
    langchain_provider: str
    api_key_env: str = ""
    api_key_kwarg: str = "api_key"
    base_url: str | None = None
    byok: bool = False
    models: list[ModelSpec] = Field(default_factory=list)

    def default_model(self) -> str:
        """Return the model marked default, else the first model."""
        for m in self.models:
            if m.default:
                return m.id
        if self.models:
            return self.models[0].id
        raise ValueError(f"Provider '{self.id}' has no models configured")


class RoleSpec(BaseModel):
    temperature: float = 0.0
    max_tokens: int | None = None


class Defaults(BaseModel):
    provider: str
    model: str


class ModelConfig(BaseModel):
    defaults: Defaults
    providers: list[ProviderSpec]
    roles: dict[str, RoleSpec] = Field(default_factory=dict)

    def provider(self, provider_id: str) -> ProviderSpec | None:
        return next((p for p in self.providers if p.id == provider_id), None)

    def role(self, role: str) -> RoleSpec:
        return self.roles.get(role, RoleSpec())


def _config_path() -> pathlib.Path:
    override = os.environ.get("MODELS_CONFIG_PATH")
    return pathlib.Path(override) if override else _DEFAULT_CONFIG_PATH


@functools.lru_cache(maxsize=1)
def load_model_config() -> ModelConfig:
    """Load + validate the model config (cached)."""
    path = _config_path()
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    config = ModelConfig.model_validate(raw)
    # Sanity check: the configured default must resolve to a real provider.
    if config.provider(config.defaults.provider) is None:
        raise ValueError(
            f"defaults.provider '{config.defaults.provider}' is not a known provider"
        )
    return config


def reload_model_config() -> ModelConfig:
    """Clear the cache and reload (used in tests)."""
    load_model_config.cache_clear()
    return load_model_config()


def get_provider(provider_id: str) -> ProviderSpec:
    """Return a provider spec, falling back to the configured default provider."""
    config = load_model_config()
    spec = config.provider(provider_id)
    if spec is None:
        spec = config.provider(config.defaults.provider)
    assert spec is not None  # default validated at load time
    return spec


def get_role_spec(role: str) -> RoleSpec:
    return load_model_config().role(role)


def get_default() -> Defaults:
    return load_model_config().defaults


def public_registry() -> list[dict[str, object]]:
    """Provider/model registry for the frontend — no secrets, no env values."""
    config = load_model_config()
    out: list[dict[str, object]] = []
    for p in config.providers:
        out.append(
            {
                "id": p.id,
                "label": p.label or p.id,
                "byok": p.byok,
                "default_model": p.default_model() if p.models else "",
                "models": [
                    {"id": m.id, "label": m.label or m.id, "default": m.default}
                    for m in p.models
                ],
            }
        )
    return out


__all__ = [
    "ModelConfig",
    "ModelSpec",
    "ProviderSpec",
    "RoleSpec",
    "load_model_config",
    "reload_model_config",
    "get_provider",
    "get_role_spec",
    "get_default",
    "public_registry",
]
