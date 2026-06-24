"""Role-aware, multi-provider LLM resolution — the one place agents call.

Combines three sources:
  • config/models.yaml (via config.py) — providers, models, per-role temps
  • an optional per-request LLMContext   — the user's provider/model choice + key
  • environment variables                — fallback API keys

Agents call ``get_llm_for_role("developer", ctx)`` instead of constructing models
themselves, so model/temperature changes happen in YAML and provider/key changes
happen at the request boundary.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel

from shared.providers import build_chat_model
from src.dev_agent import config

logger = logging.getLogger(__name__)


@dataclass
class LLMContext:
    """Per-request LLM selection. ``api_key`` is the resolved BYOK key (plaintext,
    in-memory only) — None means fall back to the server's env var."""

    provider: str | None = None
    model: str | None = None
    client_id: str | None = None
    api_key: str | None = None


def get_llm_for_role(role: str, ctx: LLMContext | None = None) -> BaseChatModel:
    """Build the chat model for an agent role, honoring any per-request override."""
    role_spec = config.get_role_spec(role)
    defaults = config.get_default()

    provider_id = (ctx.provider if ctx and ctx.provider else None) or defaults.provider
    provider_spec = config.get_provider(provider_id)

    if ctx and ctx.model:
        model = ctx.model
    elif provider_id == defaults.provider:
        model = defaults.model
    else:
        model = provider_spec.default_model()

    # Key resolution: BYOK (already resolved into ctx) → server env var.
    api_key = (ctx.api_key if ctx else None) or (
        os.environ.get(provider_spec.api_key_env) if provider_spec.api_key_env else None
    )

    return build_chat_model(
        langchain_provider=provider_spec.langchain_provider,
        model=model,
        temperature=role_spec.temperature,
        max_tokens=role_spec.max_tokens,
        api_key=api_key,
        api_key_kwarg=provider_spec.api_key_kwarg,
        base_url=provider_spec.base_url,
    )


async def build_llm_context(
    client_id: str | None,
    provider: str | None,
    model: str | None,
    transient_key: str | None = None,
) -> LLMContext:
    """Resolve a request's LLM selection into an LLMContext.

    Looks up the user's stored (encrypted) BYOK key for the chosen provider when a
    ``client_id`` is present and no transient key was supplied. Decryption happens
    here, once per request, so agents never touch the DB or the vault.
    """
    api_key = transient_key
    if api_key is None and client_id and provider:
        try:
            from src.dev_agent.db.database import async_session_factory
            from src.dev_agent.db.keys_store import get_provider_key

            async with async_session_factory() as db:
                api_key = await get_provider_key(db, client_id, provider)
        except Exception as exc:  # DB/vault optional — fall back to env
            logger.warning("Could not load stored API key: %s", exc)

    return LLMContext(provider=provider, model=model, client_id=client_id, api_key=api_key)


__all__ = ["LLMContext", "get_llm_for_role", "build_llm_context"]
