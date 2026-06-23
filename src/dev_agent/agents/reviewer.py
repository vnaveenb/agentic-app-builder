"""Reviewer agent — final code improvements and review notes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel

from shared.providers import get_llm
from src.dev_agent.agents.retry import REVIEWER_TASKS, emit_task_done, emit_tasks, retry_llm_call
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
4. Return ONLY the files you modified in improved_files — do NOT include unchanged files
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
        await emit_tasks(queue, "reviewer", REVIEWER_TASKS)

    await emit_task_done(queue, "reviewer", 0)  # Reviewing code quality

    # Build file summary — send full content so the LLM can review properly
    files_summary = ""
    for fname, code in files.items():
        files_summary += f"\n--- {fname} ---\n{code}\n"

    test_summary = "No tests run"
    tr = state.get("test_report")
    if tr is not None:
        test_summary = f"passed={tr.passed_count}, failed={tr.failed_count}, critical_bugs={tr.has_critical_bugs}"

    prompt = _REVIEWER_PROMPT.format(
        app_name=plan.app_name,
        runtime=state["runtime"],
        test_summary=test_summary,
        files_summary=files_summary[:30000],
    )

    llm = get_llm(temperature=0.3)
    structured_llm = llm.with_structured_output(_ReviewOutput)
    result: _ReviewOutput = await retry_llm_call(
        structured_llm.ainvoke,
        prompt,
        agent_name="reviewer",
        queue=queue,
        task_id=1,
        task_text="Applying improvements",
    )  # type: ignore[assignment]

    await emit_task_done(queue, "reviewer", 1)  # Applying improvements

    # Merge: start with ALL original files, then overlay reviewer's modifications.
    # This prevents the old bug where unmodified files were silently dropped.
    merged_files = dict(files)
    merged_files.update(result.improved_files)

    await emit_task_done(queue, "reviewer", 2)  # Generating review notes

    logger.info("Reviewer modified %d/%d files, %d notes",
                len(result.improved_files), len(files), len(result.review_notes))

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
        "files": merged_files,
        "review_notes": result.review_notes,
        "status": "done",
        "current_agent": "reviewer",
    }
