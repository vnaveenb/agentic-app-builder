"""Developer agent — generates multi-file code based on the plan."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from shared.providers import get_llm
from src.dev_agent.pipeline.state import DevPipelineState

logger = logging.getLogger(__name__)


class _FileEntry(BaseModel):
    filename: str
    content: str


class _DeveloperOutput(BaseModel):
    files: list[_FileEntry]
    implementation_notes: str


_DEVELOPER_PROMPT = """\
You are an expert software developer. Generate complete, working code files based on the plan below.

PROJECT PLAN:
- App: {app_name}
- Runtime: {runtime}
- Tech Stack: {tech_stack}
- Architecture: {architecture_notes}
- Tasks: {tasks}
- Expected files: {estimated_files}
- Entry point: {entry_point}

RUNTIME-SPECIFIC RULES:
{runtime_instructions}

REQUIREMENTS:
1. Generate ALL files listed in the plan
2. Code must be complete and runnable — no placeholders or TODOs
3. Include a test file if runtime supports it
4. Entry point must work as specified

{feedback_section}

Generate the complete file set now.
"""

_RUNTIME_INSTRUCTIONS = {
    "python": """- Include requirements.txt with all dependencies
- Entry point must bind to PORT environment variable (default 8000)
- Use: app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))
- Flask apps MUST have a @app.route('/') that serves the main HTML page
- Use render_template_string or inline HTML for the root route — do NOT rely on templates/ folder
- Include test_*.py file with pytest-compatible tests""",
    "node": """- Include package.json with dependencies and start script
- Entry point must listen on process.env.PORT (default 3000)
- Include test.js using Node 20 built-in test runner (node:test)
- Use: const port = process.env.PORT || 3000; server.listen(port)""",
    "react": """- Use CDN imports: <script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
- Use CDN imports: <script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
- Use Babel standalone for JSX: <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
- Everything in a single index.html file with embedded <script type="text/babel">
- Do NOT use npm, webpack, vite, or any build tools
- Do NOT generate package.json""",
    "angular": """- Use CDN imports from cdnjs.cloudflare.com for Angular
- Everything in a single index.html file with embedded scripts
- Do NOT use npm, ng CLI, or any build tools
- Do NOT generate package.json""",
    "static": """- Pure HTML/CSS/JS, no frameworks
- Entry point is index.html
- Can use multiple .js and .css files
- No build step required""",
}

_FEEDBACK_TEMPLATE = """
⚠️ PREVIOUS ITERATION FAILED — FIX THESE BUGS:
{output_summary}

Regenerate the files with these specific issues fixed. Keep everything else the same.
"""

_USER_FEEDBACK_TEMPLATE = """
⚠️ USER REQUESTED CHANGES:
{user_feedback}

Apply these changes to the existing code. Keep everything else the same unless it conflicts with the requested changes.
"""


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

    runtime = state["runtime"]
    runtime_instructions = _RUNTIME_INSTRUCTIONS.get(runtime, _RUNTIME_INSTRUCTIONS["python"])

    # On loop-back: include failure feedback
    feedback_section = ""
    tr = state.get("test_report")
    if iteration > 0 and tr is not None:
        feedback_section = _FEEDBACK_TEMPLATE.format(
            output_summary=tr.output_summary[:2000]
        )

    # User iteration feedback (from /iterate endpoint)
    if state.get("user_feedback"):
        feedback_section += _USER_FEEDBACK_TEMPLATE.format(
            user_feedback=state["user_feedback"]
        )

    prompt = _DEVELOPER_PROMPT.format(
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

    llm = get_llm(temperature=0.1, max_tokens=32768)
    structured_llm = llm.with_structured_output(_DeveloperOutput, include_raw=True)

    result: _DeveloperOutput | None = None
    last_error: Exception | None = None

    for attempt in range(3):
        try:
            raw_response = await structured_llm.ainvoke(prompt)
            if isinstance(raw_response, dict):
                if raw_response.get("parsed"):
                    result = raw_response["parsed"]
                    break
                # Fallback: try to repair truncated/malformed JSON from raw output
                raw_text = raw_response.get("raw", "")
            else:
                raw_text = raw_response
            if hasattr(raw_text, "content"):
                raw_text = raw_text.content
            result = _repair_parse(str(raw_text))
            if result:
                break
        except (ValidationError, Exception) as exc:
            last_error = exc
            logger.warning("Developer parse attempt %d failed: %s", attempt + 1, exc)
            await asyncio.sleep(1)

    if result is None:
        raise RuntimeError(
            f"Failed to parse developer output after 3 attempts: {last_error}"
        )

    # Convert to dict[str, str]
    files = {entry.filename: entry.content for entry in result.files}

    logger.info("Developer generated %d files (iteration %d)", len(files), iteration + 1)

    if queue:
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
