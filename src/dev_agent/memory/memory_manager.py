"""Memory manager — extracts learnings from completed sessions via LLM."""

from __future__ import annotations

import logging
from typing import Any

from shared.providers import get_llm
from sqlalchemy.ext.asyncio import AsyncSession

from src.dev_agent.memory.memory_store import store_memory

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """\
You are analyzing a completed software generation session to extract reusable learnings.

Session details:
- User's idea: {idea}
- Runtime: {runtime}
- Files generated: {files}
- Review notes: {review_notes}
- Test results: {test_summary}

Extract 2-5 concise learnings that would help in future sessions. Focus on:
1. User preferences (coding style, frameworks they like, patterns they prefer)
2. Successful patterns (what worked well, approaches that passed tests)
3. Common issues (errors encountered, fixes applied)

Return ONLY a JSON array of objects with these fields:
- "category": one of "preference", "pattern", "project_summary"
- "key": short descriptive key (max 50 chars)
- "value": concise description (max 200 chars)
- "relevance_score": float 0.0-1.0 (how reusable is this learning?)

Example:
[
  {{"category": "preference", "key": "react_cdn_pattern", "value": "User prefers React apps loaded from CDN without build tools", "relevance_score": 0.8}},
  {{"category": "pattern", "key": "flask_with_blueprints", "value": "Flask apps work best with blueprint structure for modularity", "relevance_score": 0.7}}
]
"""


async def extract_and_store_memories(
    db: AsyncSession,
    idea: str,
    runtime: str,
    files: dict[str, str],
    review_notes: list[str],
    test_report: dict[str, Any] | None = None,
) -> int:
    """Extract learnings from a completed session and store as memories.

    Returns the number of memories stored.
    """
    try:
        llm = get_llm(temperature=0.1)

        file_list = ", ".join(sorted(files.keys())[:20])  # Cap at 20 files
        test_summary = "No tests run"
        if test_report:
            test_summary = f"Passed: {test_report.get('passed_count', 0)}, Failed: {test_report.get('failed_count', 0)}"

        prompt = _EXTRACTION_PROMPT.format(
            idea=idea,
            runtime=runtime,
            files=file_list,
            review_notes="; ".join(review_notes[:5]) if review_notes else "None",
            test_summary=test_summary,
        )

        response = await llm.ainvoke(prompt)
        content = response.content if hasattr(response, "content") else str(response)

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
