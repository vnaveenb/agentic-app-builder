"""
LLM and embeddings factory.

Provider is selected via environment variables using LangChain's
provider:model-name format:

    LLM_PROVIDER=google_genai:gemini-2.0-flash          (default)
    LLM_PROVIDER=google_vertexai:gemini-2.0-flash-001   (Vertex AI)
    LLM_PROVIDER=openai:gpt-4o                          (swap-ready)
    LLM_PROVIDER=anthropic:claude-opus-4-8              (swap-ready)
    LLM_PROVIDER=bedrock:anthropic.claude-3-5-sonnet-20241022-v2:0  (swap-ready)
    LLM_PROVIDER=azure_openai:gpt-4o                    (swap-ready)

    EMBEDDINGS_PROVIDER=google_genai:models/text-embedding-004  (default)
    EMBEDDINGS_PROVIDER=google_vertexai:text-embedding-005      (Vertex)
    EMBEDDINGS_PROVIDER=openai:text-embedding-3-small           (swap-ready)
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, cast

from langchain.chat_models import init_chat_model
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

_DEFAULT_LLM_PROVIDER = "google_genai"
_DEFAULT_LLM_MODEL = "gemini-2.0-flash"
_DEFAULT_EMBEDDINGS_PROVIDER = "google_genai"
_DEFAULT_EMBEDDINGS_MODEL = "models/text-embedding-004"


def _parse_provider_model(
    env_value: str,
    default_provider: str,
    default_model: str,
) -> tuple[str, str]:
    """Parse 'provider:model-name' from env var, falling back to defaults."""
    if ":" in env_value:
        provider, model = env_value.split(":", 1)
        return provider.strip(), model.strip()
    return default_provider, env_value.strip()


@lru_cache(maxsize=4)
def get_llm(
    temperature: float = 0.0,
    max_tokens: int | None = None,
    *,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> BaseChatModel:
    """Return a cached chat model instance selected via LLM_PROVIDER env var.

    Pass provider_override / model_override in tests to avoid live API calls.
    """
    env_value = os.environ.get(
        "LLM_PROVIDER",
        f"{_DEFAULT_LLM_PROVIDER}:{_DEFAULT_LLM_MODEL}",
    )
    provider, model = _parse_provider_model(
        env_value, _DEFAULT_LLM_PROVIDER, _DEFAULT_LLM_MODEL
    )
    if provider_override:
        provider = provider_override
    if model_override:
        model = model_override

    kwargs: dict[str, Any] = {"temperature": temperature}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    return cast(BaseChatModel, init_chat_model(model=model, model_provider=provider, **kwargs))


@lru_cache(maxsize=2)
def get_embeddings(
    *,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> Embeddings:
    """Return a cached embeddings model instance selected via EMBEDDINGS_PROVIDER env var.

    WARNING: changing this provider/model requires re-indexing all vector stores.
    """
    env_value = os.environ.get(
        "EMBEDDINGS_PROVIDER",
        f"{_DEFAULT_EMBEDDINGS_PROVIDER}:{_DEFAULT_EMBEDDINGS_MODEL}",
    )
    provider, model = _parse_provider_model(
        env_value, _DEFAULT_EMBEDDINGS_PROVIDER, _DEFAULT_EMBEDDINGS_MODEL
    )
    if provider_override:
        provider = provider_override
    if model_override:
        model = model_override

    from langchain.embeddings import init_embeddings

    return init_embeddings(model=f"{provider}:{model}")


__all__ = ["get_llm", "get_embeddings"]
