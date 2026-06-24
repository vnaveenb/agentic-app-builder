"""Unit tests for the multi-provider config, key vault, and LLM resolver.

No network, no DB — these cover the new BYOK / model-selection plumbing.
"""

from __future__ import annotations

import os

import pytest

from src.dev_agent import config

# ── config loader ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_real_config_loads_and_default_is_modern() -> None:
    config.reload_model_config()
    defaults = config.get_default()
    assert defaults.provider == "google_genai"
    # The outdated weak default must be gone.
    assert defaults.model == "gemini-3.5-flash"
    assert config.get_provider(defaults.provider) is not None


@pytest.mark.unit
def test_get_provider_falls_back_to_default_for_unknown() -> None:
    config.reload_model_config()
    spec = config.get_provider("does-not-exist")
    assert spec.id == config.get_default().provider


@pytest.mark.unit
def test_openai_compatible_providers_have_base_url() -> None:
    config.reload_model_config()
    for pid in ("openrouter", "moonshot"):
        spec = config.get_provider(pid)
        assert spec.langchain_provider == "openai"
        assert spec.base_url, f"{pid} must define a base_url"


@pytest.mark.unit
def test_public_registry_has_no_secret_fields() -> None:
    config.reload_model_config()
    reg = config.public_registry()
    assert reg
    for prov in reg:
        assert set(prov.keys()) == {"id", "label", "byok", "default_model", "models"}


@pytest.mark.unit
def test_bad_config_is_rejected(tmp_path) -> None:
    from pydantic import ValidationError

    bad = tmp_path / "bad.yaml"
    bad.write_text("defaults:\n  provider: x\n", encoding="utf-8")  # missing model + providers
    os.environ["MODELS_CONFIG_PATH"] = str(bad)
    try:
        with pytest.raises(ValidationError):
            config.reload_model_config()
    finally:
        del os.environ["MODELS_CONFIG_PATH"]
        config.reload_model_config()


# ── key vault ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_keyvault_round_trip() -> None:
    from cryptography.fernet import Fernet

    from src.dev_agent.security import keyvault

    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    keyvault._fernet.cache_clear()
    try:
        assert keyvault.is_configured() is True
        token = keyvault.encrypt("sk-secret-123")
        assert token != b"sk-secret-123"
        assert keyvault.decrypt(token) == "sk-secret-123"
    finally:
        del os.environ["ENCRYPTION_KEY"]
        keyvault._fernet.cache_clear()


@pytest.mark.unit
def test_keyvault_disabled_without_key() -> None:
    from src.dev_agent.security import keyvault

    os.environ.pop("ENCRYPTION_KEY", None)
    keyvault._fernet.cache_clear()
    assert keyvault.is_configured() is False
    with pytest.raises(keyvault.EncryptionUnavailableError):
        keyvault.encrypt("nope")


# ── LLM resolver ──────────────────────────────────────────────────────────────


@pytest.fixture
def captured_build(monkeypatch):
    """Replace build_chat_model with a recorder so no real client is created."""
    calls: dict[str, object] = {}

    def _fake(**kwargs):  # noqa: ANN003
        calls.update(kwargs)
        return "FAKE_LLM"

    from src.dev_agent import llm

    monkeypatch.setattr(llm, "build_chat_model", _fake)
    config.reload_model_config()
    return calls


@pytest.mark.unit
def test_role_default_resolution(captured_build) -> None:
    from src.dev_agent.llm import get_llm_for_role

    result = get_llm_for_role("developer", None)
    assert result == "FAKE_LLM"
    assert captured_build["langchain_provider"] == "google_genai"
    assert captured_build["model"] == "gemini-3.5-flash"
    assert captured_build["temperature"] == 0.1
    assert captured_build["max_tokens"] == 32768
    assert captured_build["api_key_kwarg"] == "google_api_key"


@pytest.mark.unit
def test_openrouter_override_uses_base_url(captured_build) -> None:
    from src.dev_agent.llm import LLMContext, get_llm_for_role

    ctx = LLMContext(provider="openrouter", model="openai/gpt-4.1", api_key="sk-byok")
    get_llm_for_role("planner", ctx)
    assert captured_build["langchain_provider"] == "openai"
    assert captured_build["model"] == "openai/gpt-4.1"
    assert captured_build["base_url"] == "https://openrouter.ai/api/v1"
    # BYOK key wins, passed under the openai kwarg name.
    assert captured_build["api_key"] == "sk-byok"
    assert captured_build["api_key_kwarg"] == "api_key"


@pytest.mark.unit
def test_byok_key_takes_precedence_over_env(captured_build, monkeypatch) -> None:
    from src.dev_agent.llm import LLMContext, get_llm_for_role

    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    get_llm_for_role("chat", LLMContext(provider="openai", api_key="ctx-key"))
    assert captured_build["api_key"] == "ctx-key"


@pytest.mark.unit
def test_env_key_used_when_no_byok(captured_build, monkeypatch) -> None:
    from src.dev_agent.llm import LLMContext, get_llm_for_role

    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    get_llm_for_role("chat", LLMContext(provider="openai"))
    assert captured_build["api_key"] == "env-key"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_llm_context_uses_transient_key() -> None:
    from src.dev_agent.llm import build_llm_context

    ctx = await build_llm_context(None, "openai", "gpt-4o", transient_key="t-key")
    assert ctx.api_key == "t-key"
    assert ctx.provider == "openai"
    assert ctx.model == "gpt-4o"


# ── schema parsing ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_generate_request_accepts_llm_selection() -> None:
    from src.dev_agent.schemas import GenerateRequest

    req = GenerateRequest(idea="x", provider="anthropic", model="claude-opus-4-8", client_id="c1")
    assert req.provider == "anthropic"
    assert req.client_id == "c1"
    # Defaults remain optional/None.
    assert GenerateRequest(idea="y").provider is None
