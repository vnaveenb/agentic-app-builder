"""Reviewer agent — final code improvements and review notes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel

from shared.providers import get_llm
from src.dev_agent.pipeline.state import DevPipelineState

logger = logging.getLogger(__name__)


class _ReviewOutput(BaseModel):
    improved_files: dict[str, str]
    review_notes: list[str]


_REVIEWER_PROMPT = """\
You are a senior software reviewer. Review the following generated code and make improvements.

App: {app_name}
Runtime: {runtime}
Test Results: {test_summary}

Files to review:
{files_summary}

Your job:
1. Fix any remaining minor issues (formatting, naming, edge cases)
2. Add helpful comments where code is complex
3. Ensure the code follows best practices for the runtime
4. Return ALL files (even unchanged ones) in improved_files
5. Provide review_notes: a list of 3-5 observations about the code quality

Do NOT make breaking changes. Keep the same functionality.
"""


async def reviewer_node(state: DevPipelineState) -> dict[str, Any]:
    """LangGraph node: review and improve generated code."""
    queue: asyncio.Queue | None = state.get("event_queue")
    files = state["files"]
    plan = state["plan"]
    if plan is None:
        raise ValueError("Plan is missing")

    if queue:
        await queue.put({"event": "agent_start", "agent": "reviewer"})

    # Build file summary (truncate for token limits)
    files_summary = ""
    for fname, code in files.items():
        truncated = code[:3000] + ("..." if len(code) > 3000 else "")
        files_summary += f"\n--- {fname} ---\n{truncated}\n"

    test_summary = "No tests run"
    tr = state.get("test_report")
    if tr is not None:
        test_summary = f"passed={tr.passed_count}, failed={tr.failed_count}, critical_bugs={tr.has_critical_bugs}"

    prompt = _REVIEWER_PROMPT.format(
        app_name=plan.app_name,
        runtime=state["runtime"],
        test_summary=test_summary,
        files_summary=files_summary[:15000],
    )

    llm = get_llm(temperature=0.3)
    structured_llm = llm.with_structured_output(_ReviewOutput)
    result: _ReviewOutput = await structured_llm.ainvoke(prompt)  # type: ignore[assignment]

    logger.info("Reviewer produced %d files, %d notes",
                len(result.improved_files), len(result.review_notes))

    if queue:
        await queue.put({
            "event": "agent_output",
            "agent": "reviewer",
            "data": {
                "review_notes": result.review_notes,
                "files_improved": list(result.improved_files.keys()),
            },
        })
        await queue.put({
            "event": "agent_complete",
            "agent": "reviewer",
            "data": {"notes_count": len(result.review_notes)},
        })

    return {
        "files": result.improved_files,
        "review_notes": result.review_notes,
        "status": "done",
        "current_agent": "reviewer",
    }
