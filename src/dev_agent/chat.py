"""Chat service — handles conversational interaction with the AI agent."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.dev_agent.agents.prompts import CHAT_SYSTEM_PROMPT
from src.dev_agent.db.models import Message
from src.dev_agent.llm import LLMContext, get_llm_for_role
from src.dev_agent.memory.memory_store import format_memories_for_prompt, get_relevant_memories

logger = logging.getLogger(__name__)

_ITERATE_TRIGGER = "[ACTION:ITERATE]"


async def store_message(
    db: AsyncSession,
    session_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> Message:
    """Persist a chat message to the database."""
    msg = Message(
        id=uuid.uuid4(),
        session_id=uuid.UUID(session_id),
        role=role,
        content=content,
        metadata_=metadata or {},
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


async def get_chat_history(db: AsyncSession, session_id: str, limit: int = 50) -> list[Message]:
    """Retrieve chat history for a session."""
    result = await db.execute(
        select(Message)
        .where(Message.session_id == uuid.UUID(session_id))
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def generate_chat_response(
    db: AsyncSession,
    session_id: str,
    user_message: str,
    session_context: dict[str, Any],
    ctx: LLMContext | None = None,
) -> tuple[str, bool]:
    """Generate an AI response to a user chat message.

    Returns (response_text, should_iterate).
    should_iterate is True if the assistant's response triggers a code iteration.
    """
    # Get memory context
    try:
        memories = await get_relevant_memories(db, limit=5)
        memory_text = format_memories_for_prompt(memories)
        if memory_text:
            memory_context = f"User memory (learned from past sessions):\n{memory_text}"
        else:
            memory_context = ""
    except Exception:
        memory_context = ""

    # Build conversation history
    history = await get_chat_history(db, session_id, limit=10)
    messages: list[dict[str, str]] = []

    # System prompt with session context
    files_list = ", ".join(sorted(session_context.get("files", {}).keys())[:15])
    system_prompt = CHAT_SYSTEM_PROMPT.format(
        idea=session_context.get("idea", "Not set"),
        runtime=session_context.get("runtime", "auto"),
        status=session_context.get("status", "unknown"),
        files=files_list or "None generated yet",
        memory_context=memory_context,
    )
    messages.append({"role": "system", "content": system_prompt})

    # Add recent history
    for msg in history[-8:]:  # Last 8 messages for context window
        messages.append({"role": msg.role, "content": msg.content})

    # Add current user message
    messages.append({"role": "user", "content": user_message})

    # Call LLM
    llm = get_llm_for_role("chat", ctx)
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    lc_messages = []
    for m in messages:
        if m["role"] == "system":
            lc_messages.append(SystemMessage(content=m["content"]))
        elif m["role"] == "user":
            lc_messages.append(HumanMessage(content=m["content"]))
        elif m["role"] == "assistant":
            lc_messages.append(AIMessage(content=m["content"]))

    response = await llm.ainvoke(lc_messages)
    response_text = response.content if hasattr(response, "content") else str(response)

    # Check if the response triggers an iteration
    should_iterate = _ITERATE_TRIGGER in response_text
    if should_iterate:
        # Strip the trigger from the visible response
        response_text = response_text.replace(_ITERATE_TRIGGER, "").strip()

    return response_text, should_iterate
