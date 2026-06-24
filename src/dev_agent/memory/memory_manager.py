"""Memory manager — extracts learnings from completed sessions via LLM."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.dev_agent.agents.prompts import MEMORY_EXTRACTION_PROMPT
from src.dev_agent.llm import LLMContext, get_llm_for_role
from src.dev_agent.memory.memory_store import store_memory
from shared.adapter import extract_text

logger = logging.getLogger(__name__)


async def extract_and_store_memories(
    db: AsyncSession,
    idea: str,
    runtime: str,
    files: dict[str, str],
    review_notes: list[str],
    test_report: dict[str, Any] | None = None,
    ctx: LLMContext | None = None,
) -> int:
    """Extract learnings from a completed session and store as memories.

    Returns the number of memories stored.
    """
    try:
        llm = get_llm_for_role("memory", ctx)

        file_list = ", ".join(sorted(files.keys())[:20])  # Cap at 20 files
        test_summary = "No tests run"
        if test_report:
            test_summary = f"Passed: {test_report.get('passed_count', 0)}, Failed: {test_report.get('failed_count', 0)}"

        prompt = MEMORY_EXTRACTION_PROMPT.format(
            idea=idea,
            runtime=runtime,
            files=file_list,
            review_notes="; ".join(review_notes[:5]) if review_notes else "None",
            test_summary=test_summary,
        )

        response = await llm.ainvoke(prompt)
        content = extract_text(response) if hasattr(response, "content") else str(response)

        # Parse JSON from response
        import json
        # Handle markdown code blocks
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        learnings = json.loads(content.strip())
        stored = 0

        for learning in learnings:
            if not isinstance(learning, dict):
                continue
            category = learning.get("category", "pattern")
            key = learning.get("key", "")
            value = learning.get("value", "")
            score = float(learning.get("relevance_score", 0.5))

            if key and value:
                await store_memory(db, category, key, value, score)
                stored += 1

        logger.info("Extracted and stored %d memories from session", stored)
        return stored

    except Exception as exc:
        logger.warning("Memory extraction failed (non-critical): %s", exc)
        return 0
