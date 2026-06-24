"""Designer agent — enhances visual design and styling of generated code."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel

from src.dev_agent.agents.prompts import DESIGNER_PROMPT
from src.dev_agent.agents.retry import DESIGNER_TASKS, emit_task_done, emit_tasks, retry_llm_call
from src.dev_agent.llm import get_llm_for_role
from src.dev_agent.pipeline.state import DevPipelineState

logger = logging.getLogger(__name__)


class _DesignerOutput(BaseModel):
    improved_files: dict[str, str]
    design_notes: list[str] = []


async def designer_node(state: DevPipelineState) -> dict[str, Any]:
    """LangGraph node: enhance visual design of generated code."""
    queue: asyncio.Queue | None = state.get("event_queue")
    files = state["files"]
    runtime = state["runtime"]

    if queue:
        await queue.put({"event": "agent_start", "agent": "designer"})
        await emit_tasks(queue, "designer", DESIGNER_TASKS)

    await emit_task_done(queue, "designer", 0)

    files_summary = ""
    for fname, code in files.items():
        files_summary += f"\n--- {fname} ---\n{code}\n"

    prompt = DESIGNER_PROMPT.format(
        runtime=runtime,
        files_summary=files_summary[:30000],
    )

    llm = get_llm_for_role("reviewer", state.get("llm_context"))
    structured_llm = llm.with_structured_output(_DesignerOutput)
    result: _DesignerOutput = await retry_llm_call(
        structured_llm.ainvoke,
        prompt,
        agent_name="designer",
        queue=queue,
        task_id=1,
        task_text="Enhancing visual design",
    )  # type: ignore[assignment]

    await emit_task_done(queue, "designer", 1)

    merged_files = dict(files)
    merged_files.update(result.improved_files)

    await emit_task_done(queue, "designer", 2)

    logger.info("Designer enhanced %d/%d files", len(result.improved_files), len(files))

    if queue:
        await queue.put({
            "event": "agent_output",
            "agent": "designer",
            "data": {
                "files_enhanced": list(result.improved_files.keys()),
                "design_notes": result.design_notes,
            },
        })
        await queue.put({
            "event": "files_update",
            "agent": "designer",
            "files": merged_files,
        })
        await queue.put({
            "event": "agent_complete",
            "agent": "designer",
            "data": {"files_enhanced": len(result.improved_files)},
        })

    return {
        "files": merged_files,
        "current_agent": "designer",
    }
