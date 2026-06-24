"""Developer agent — generates multi-file code based on the plan."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from src.dev_agent.agents.prompts import (
    DEVELOPER_PROMPT,
    FEEDBACK_TEMPLATE,
    RUNTIME_INSTRUCTIONS,
    USER_FEEDBACK_TEMPLATE,
)
from src.dev_agent.agents.retry import DEVELOPER_TASKS, emit_task_done, emit_tasks, retry_llm_call
from src.dev_agent.llm import get_llm_for_role
from src.dev_agent.pipeline.state import DevPipelineState

logger = logging.getLogger(__name__)


class _FileEntry(BaseModel):
    filename: str
    content: str


class _DeveloperOutput(BaseModel):
    files: list[_FileEntry]
    implementation_notes: str


def _repair_parse(raw: str) -> _DeveloperOutput | None:
    """Attempt to parse _DeveloperOutput from raw LLM text that may be truncated."""
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())

    # Try direct parse
    try:
        return _DeveloperOutput.model_validate_json(raw)
    except (ValidationError, json.JSONDecodeError):
        pass

    # Try to fix truncated JSON by closing open structures
    try:
        patched = raw.rstrip()
        # Close any open string
        if patched.count('"') % 2 != 0:
            patched += '"'
        # Close open array/object brackets
        open_braces = patched.count("{") - patched.count("}")
        open_brackets = patched.count("[") - patched.count("]")
        patched += "]" * max(open_brackets, 0)
        patched += "}" * max(open_braces, 0)

        data = json.loads(patched)
        # Ensure implementation_notes exists
        if "implementation_notes" not in data:
            data["implementation_notes"] = ""
        return _DeveloperOutput.model_validate(data)
    except (ValidationError, json.JSONDecodeError, Exception) as exc:
        logger.debug("Repair parse failed: %s", exc)
        return None


async def developer_node(state: DevPipelineState) -> dict[str, Any]:
    """LangGraph node: generate code files based on the plan."""
    queue: asyncio.Queue | None = state.get("event_queue")
    plan = state["plan"]
    if plan is None:
        raise ValueError("Plan is missing")
    iteration = state["iteration"]

    if queue:
        await queue.put({"event": "agent_start", "agent": "developer", "data": {"iteration": iteration + 1}})
        await emit_tasks(queue, "developer", DEVELOPER_TASKS)

    await emit_task_done(queue, "developer", 0)  # Preparing prompt

    runtime = state["runtime"]
    runtime_instructions = RUNTIME_INSTRUCTIONS.get(runtime, RUNTIME_INSTRUCTIONS["python"])

    # On loop-back: include failure feedback
    feedback_section = ""
    tr = state.get("test_report")
    if iteration > 0 and tr is not None:
        feedback_section = FEEDBACK_TEMPLATE.format(
            output_summary=tr.output_summary[:2000]
        )

    # User iteration feedback (from /iterate endpoint)
    if state.get("user_feedback"):
        feedback_section += USER_FEEDBACK_TEMPLATE.format(
            user_feedback=state["user_feedback"]
        )

    prompt = DEVELOPER_PROMPT.format(
        app_name=plan.app_name,
        runtime=runtime,
        tech_stack=", ".join(plan.tech_stack),
        architecture_notes=plan.architecture_notes,
        tasks="\n".join(f"- {t.title}: {t.description}" for t in plan.tasks),
        estimated_files=", ".join(plan.estimated_files),
        entry_point=plan.entry_point,
        runtime_instructions=runtime_instructions,
        feedback_section=feedback_section,
    )

    llm = get_llm_for_role("developer", state.get("llm_context"))
    structured_llm = llm.with_structured_output(_DeveloperOutput, include_raw=True)

    result: _DeveloperOutput | None = None
    last_error: Exception | None = None

    # Retry with exponential backoff for transient API errors (503/429)
    try:
        raw_response = await retry_llm_call(
            structured_llm.ainvoke,
            prompt,
            agent_name="developer",
            queue=queue,
            task_id=1,
            task_text="Generating code",
        )
        if isinstance(raw_response, dict):
            if raw_response.get("parsed"):
                result = raw_response["parsed"]
            else:
                raw_text = raw_response.get("raw", "")
                if hasattr(raw_text, "content"):
                    raw_text = raw_text.content
                result = _repair_parse(str(raw_text))
        else:
            raw_text = raw_response
            if hasattr(raw_text, "content"):
                raw_text = raw_text.content
            result = _repair_parse(str(raw_text))
    except (ValidationError, Exception) as exc:
        last_error = exc
        logger.warning("Developer generation failed: %s", exc)

    if result is None:
        raise RuntimeError(
            f"Failed to parse developer output: {last_error}"
        )

    await emit_task_done(queue, "developer", 1)  # Generating code
    await emit_task_done(queue, "developer", 2)  # Parsing & validating output

    # Convert to dict[str, str]
    files = {entry.filename: entry.content for entry in result.files}

    logger.info("Developer generated %d files (iteration %d)", len(files), iteration + 1)

    if queue:
        await queue.put({
            "event": "files_update",
            "agent": "developer",
            "files": files,
        })
        await queue.put({
            "event": "agent_output",
            "agent": "developer",
            "data": {
                "files_generated": list(files.keys()),
                "implementation_notes": result.implementation_notes[:500],
                "iteration": iteration + 1,
            },
        })
        await queue.put({
            "event": "agent_complete",
            "agent": "developer",
            "data": {"file_count": len(files), "iteration": iteration + 1},
        })

    return {
        "files": files,
        "iteration": iteration + 1,
        "current_agent": "developer",
    }
