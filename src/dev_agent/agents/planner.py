"""Planner agent — takes an idea, produces a structured Plan with runtime detection."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.dev_agent.agents.prompts import PLANNER_PROMPT
from src.dev_agent.agents.retry import PLANNER_TASKS, emit_task_done, emit_tasks, retry_llm_call
from src.dev_agent.llm import get_llm_for_role
from src.dev_agent.pipeline.state import DevPipelineState, Plan

logger = logging.getLogger(__name__)


async def planner_node(state: DevPipelineState) -> dict[str, Any]:
    """LangGraph node: plan the project based on the user's idea."""
    queue: asyncio.Queue | None = state.get("event_queue")

    if queue:
        await queue.put({"event": "agent_start", "agent": "planner"})
        await emit_tasks(queue, "planner", PLANNER_TASKS)

    await emit_task_done(queue, "planner", 0)  # Analyzing requirements

    llm = get_llm_for_role("planner", state.get("llm_context"))
    structured_llm = llm.with_structured_output(Plan)

    # Inject memory context if available
    memory_context = ""
    try:
        from src.dev_agent.db.database import async_session_factory
        from src.dev_agent.memory.memory_store import (
            format_memories_for_prompt,
            get_relevant_memories,
        )

        async with async_session_factory() as db:
            memories = await get_relevant_memories(db, limit=5)
            memory_text = format_memories_for_prompt(memories)
            if memory_text:
                memory_context = f"\n\nContext from previous sessions (user preferences):\n{memory_text}"
    except Exception:
        pass  # Memory is optional — don't break the pipeline

    prompt = PLANNER_PROMPT.format(idea=state["idea"]) + memory_context

    await emit_task_done(queue, "planner", 1)  # Designing architecture

    plan: Plan = await retry_llm_call(
        structured_llm.ainvoke,
        prompt,
        agent_name="planner",
        queue=queue,
        task_id=1,
        task_text="Designing architecture",
    )  # type: ignore[assignment]

    await emit_task_done(queue, "planner", 2)  # Defining file structure

    logger.info("Plan created: app=%s, runtime=%s, files=%d",
                plan.app_name, plan.runtime, len(plan.estimated_files))

    if queue:
        await queue.put({
            "event": "agent_output",
            "agent": "planner",
            "data": {
                "app_name": plan.app_name,
                "runtime": plan.runtime,
                "tech_stack": plan.tech_stack,
                "architecture": plan.architecture_notes,
                "files": plan.estimated_files,
                "entry_point": plan.entry_point,
                "tasks": [t.title for t in plan.tasks],
            },
        })
        await queue.put({
            "event": "agent_complete",
            "agent": "planner",
            "data": {"app_name": plan.app_name, "runtime": plan.runtime},
        })

    return {
        "plan": plan,
        "runtime": plan.runtime,
        "current_agent": "planner",
    }
