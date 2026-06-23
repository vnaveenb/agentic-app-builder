"""Memory store — CRUD operations for cross-session memory persistence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.dev_agent.db.models import Memory


async def store_memory(
    db: AsyncSession,
    category: str,
    key: str,
    value: str,
    relevance_score: float = 1.0,
) -> Memory:
    """Store a new memory or update existing one with same key."""
    # Check if key already exists
    result = await db.execute(
        select(Memory).where(Memory.key == key, Memory.category == category)
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.value = value
        existing.relevance_score = relevance_score
        existing.last_accessed = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(existing)
        return existing

    memory = Memory(
        id=uuid.uuid4(),
        category=category,
        key=key,
        value=value,
        relevance_score=relevance_score,
    )
    db.add(memory)
    await db.commit()
    await db.refresh(memory)
    return memory


async def get_all_memories(db: AsyncSession) -> list[Memory]:
    """Get all stored memories, ordered by relevance."""
    result = await db.execute(
        select(Memory).order_by(Memory.relevance_score.desc(), Memory.last_accessed.desc())
    )
    return list(result.scalars().all())


async def get_memories_by_category(db: AsyncSession, category: str) -> list[Memory]:
    """Get memories filtered by category."""
    result = await db.execute(
        select(Memory)
        .where(Memory.category == category)
        .order_by(Memory.relevance_score.desc())
    )
    return list(result.scalars().all())


async def get_relevant_memories(db: AsyncSession, limit: int = 10) -> list[Memory]:
    """Get top N most relevant memories for prompt injection."""
    result = await db.execute(
        select(Memory)
        .order_by(Memory.relevance_score.desc(), Memory.access_count.desc())
        .limit(limit)
    )
    memories = list(result.scalars().all())

    # Update access counts
    for mem in memories:
        mem.access_count += 1
        mem.last_accessed = datetime.now(timezone.utc)
    await db.commit()

    return memories


async def delete_memory(db: AsyncSession, memory_id: str) -> bool:
    """Delete a specific memory by ID."""
    result = await db.execute(
        delete(Memory).where(Memory.id == uuid.UUID(memory_id))
    )
    await db.commit()
    return result.rowcount > 0  # type: ignore[return-value]


async def clear_all_memories(db: AsyncSession) -> int:
    """Delete all memories. Returns count of deleted entries."""
    result = await db.execute(delete(Memory))
    await db.commit()
    return result.rowcount  # type: ignore[return-value]


def format_memories_for_prompt(memories: list[Memory]) -> str:
    """Format memories into a string for LLM prompt injection."""
    if not memories:
        return ""

    sections: dict[str, list[str]] = {}
    for mem in memories:
        sections.setdefault(mem.category, []).append(f"- {mem.key}: {mem.value}")

    parts = []
    for category, items in sections.items():
        parts.append(f"[{category.upper()}]")
        parts.extend(items)
        parts.append("")

    return "\n".join(parts)
