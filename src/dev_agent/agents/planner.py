"""Planner agent — takes an idea, produces a structured Plan with runtime detection."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from shared.providers import get_llm
from src.dev_agent.agents.retry import PLANNER_TASKS, emit_task_done, emit_tasks, retry_llm_call
from src.dev_agent.pipeline.state import DevPipelineState, Plan

logger = logging.getLogger(__name__)

_PLANNER_PROMPT = """\
You are a software architect. Given a user's app idea, produce a detailed project plan.

IMPORTANT — Runtime detection rules:
- "python" → for Flask, FastAPI, Django, scripts, CLI tools, data apps
- "node" → for Express, Koa, Hapi, backend JavaScript/TypeScript servers
- "react" → for React UI apps (use CDN imports from unpkg.com/react — do NOT use npm/webpack/vite)
- "angular" → for Angular UI apps (use CDN imports from cdnjs.cloudflare.com — do NOT use npm/ng CLI)
- "static" → for plain HTML/CSS/JS pages, landing pages, portfolios

For React and Angular: generate a single index.html that loads the framework from CDN.
Do NOT generate package.json or require any build step for React/Angular projects.

The user's idea: {idea}

Produce a plan with:
- app_name: short snake_case name
- runtime: one of python/node/react/angular/static
- tech_stack: list of technologies used
- tasks: list of implementation tasks
- architecture_notes: brief architecture description
- estimated_files: list of filenames that will be generated
- entry_point: the main file to run/serve (e.g. main.py, server.js, index.html)
"""


async def planner_node(state: DevPipelineState) -> dict[str, Any]:
    """LangGraph node: plan the project based on the user's idea."""
    queue: asyncio.Queue | None = state.get("event_queue")

    if queue:
        await queue.put({"event": "agent_start", "agent": "planner"})
        await emit_tasks(queue, "planner", PLANNER_TASKS)

    await emit_task_done(queue, "planner", 0)  # Analyzing requirements

    llm = get_llm(temperature=0.2)
    structured_llm = llm.with_structured_output(Plan)

    # Inject memory context if available
    memory_context = ""
    try:
        from src.dev_agent.db.database import async_session_factory
        from src.dev_agent.memory.memory_store import format_memories_for_prompt, get_relevant_memories

        async with async_session_factory() as db:
            memories = await get_relevant_memories(db, limit=5)
            memory_text = format_memories_for_prompt(memories)
            if memory_text:
                memory_context = f"\n\nContext from previous sessions (user preferences):\n{memory_text}"
    except Exception:
        pass  # Memory is optional — don't break the pipeline

    prompt = _PLANNER_PROMPT.format(idea=state["idea"]) + memory_context

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
