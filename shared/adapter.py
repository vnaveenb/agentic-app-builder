"""Provider adapter layer.

Quarantines provider-specific quirks (safety filters, tool-call edge cases,
multi-part content blocks) so business logic never sees them.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Unrecoverable provider error."""


class SafetyFilterError(ProviderError):
    """Content blocked by provider safety filters."""


class ToolCallError(ProviderError):
    """Malformed tool-call response from provider."""


def extract_text(message: BaseMessage) -> str:
    """Safely extract text from any BaseMessage, including multi-part Gemini responses."""
    if isinstance(message.content, str):
        return message.content
    if isinstance(message.content, list):
        parts = []
        for block in message.content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(message.content)


def check_safety_filter(message: AIMessage) -> None:
    """Raise SafetyFilterError if the response was blocked.

    Gemini uses finish_reason='SAFETY'; OpenAI uses 'content_filter'.
    """
    metadata = getattr(message, "response_metadata", {})
    finish_reason = metadata.get("finish_reason", "")
    if finish_reason in ("SAFETY", "content_filter"):
        raise SafetyFilterError(
            f"Content blocked by provider safety filter (finish_reason={finish_reason!r})"
        )


def normalise_tool_calls(message: AIMessage) -> list[dict[str, Any]]:
    """Return tool calls in a consistent format regardless of provider."""
    raw = getattr(message, "tool_calls", [])
    result = []
    for tc in raw:
        if not isinstance(tc, dict):
            logger.warning("Unexpected tool_call type: %s", type(tc))
            continue
        result.append(
            {
                "id": tc.get("id", ""),
                "name": tc.get("name", ""),
                "args": tc.get("args", {}),
            }
        )
    return result


__all__ = [
    "ProviderError",
    "SafetyFilterError",
    "ToolCallError",
    "extract_text",
    "check_safety_filter",
    "normalise_tool_calls",
]
