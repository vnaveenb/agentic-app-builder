"""CrewAI pipeline backend — role-based agent orchestration.

Implements the same 4-agent pipeline (planner → developer → tester → reviewer)
using CrewAI's role-based paradigm instead of LangGraph's state-machine approach.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from src.dev_agent.agents.prompts import (
    DEVELOPER_PROMPT,
    FEEDBACK_TEMPLATE,
    PLANNER_PROMPT,
    REVIEWER_PROMPT,
    RUNTIME_INSTRUCTIONS,
    STATIC_ANALYSIS_PROMPT,
    USER_FEEDBACK_TEMPLATE,
)
from src.dev_agent.llm import get_llm_for_role
from src.dev_agent.pipeline.base import PipelineBackend
from src.dev_agent.pipeline.executor import run_tests_in_sandbox
from src.dev_agent.pipeline.state import DevPipelineState, Plan, TestReport

logger = logging.getLogger(__name__)

# Attempt to import CrewAI — graceful fallback if not installed
CREWAI_AVAILABLE = importlib.util.find_spec("crewai") is not None


# ── Helper: parse developer files from raw text ──────────────────────────────

class _FileEntry(BaseModel):
    filename: str
    content: str


class _DeveloperOutput(BaseModel):
    files: list[_FileEntry]
    implementation_notes: str


class _ReviewOutput(BaseModel):
    improved_files: dict[str, str]
    review_notes: list[str]


def _repair_parse(raw: str) -> _DeveloperOutput | None:
    """Attempt to parse _DeveloperOutput from raw LLM text."""
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())
    try:
        return _DeveloperOutput.model_validate_json(raw)
    except (ValidationError, json.JSONDecodeError):
        pass
    try:
        patched = raw.rstrip()
        if patched.count('"') % 2 != 0:
            patched += '"'
        open_braces = patched.count("{") - patched.count("}")
        open_brackets = patched.count("[") - patched.count("]")
        patched += "]" * max(open_brackets, 0)
        patched += "}" * max(open_braces, 0)
        data = json.loads(patched)
        if "implementation_notes" not in data:
            data["implementation_notes"] = ""
        return _DeveloperOutput.model_validate(data)
    except Exception as exc:
        logger.debug("Repair parse failed: %s", exc)
        return None


# ── CrewAI Backend ────────────────────────────────────────────────────────────


class CrewAIBackend(PipelineBackend):
    """CrewAI-based orchestration backend (role-based collaboration)."""

    async def run(
        self,
        state: DevPipelineState,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> DevPipelineState:
        """Execute the full pipeline: planner → developer → tester → reviewer."""
        # Phase 1: Plan
        state = await self._run_planner(state, queue)
        # Phase 2-4: Developer → Tester → Reviewer (with potential loops)
        state = await self._run_dev_test_review_loop(state, queue)
        return state

    async def run_iterate(
        self,
        state: DevPipelineState,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> DevPipelineState:
        """Execute iterate pipeline: developer → tester → reviewer (skip planner)."""
        state = await self._run_dev_test_review_loop(state, queue)
        return state

    # ── Planner ───────────────────────────────────────────────────────────────

    async def _run_planner(
        self,
        state: DevPipelineState,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> DevPipelineState:
        await queue.put({"event": "agent_start", "agent": "planner"})

        llm = get_llm_for_role("planner", state.get("llm_context"))
        structured_llm = llm.with_structured_output(Plan)

        prompt = PLANNER_PROMPT.format(idea=state["idea"])
        plan: Plan = await structured_llm.ainvoke(prompt)  # type: ignore[assignment]

        logger.info("[CrewAI] Plan created: app=%s, runtime=%s", plan.app_name, plan.runtime)

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

        state["plan"] = plan
        state["runtime"] = plan.runtime
        state["current_agent"] = "planner"
        return state

    # ── Developer → Tester → Reviewer loop ────────────────────────────────────

    async def _run_dev_test_review_loop(
        self,
        state: DevPipelineState,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> DevPipelineState:
        """Run the developer → tester → reviewer cycle with test-failure loop-back."""
        plan = state["plan"]
        if plan is None:
            raise ValueError("Plan is missing — cannot run dev loop")

        max_iterations = state["max_iterations"]

        for iteration in range(max_iterations):
            # ── Developer ─────────────────────────────────────────────────
            state = await self._run_developer(state, queue, iteration)

            # ── Tester ────────────────────────────────────────────────────
            state = await self._run_tester(state, queue)

            # Check: should we loop back?
            tr = state.get("test_report")
            if tr and tr.has_critical_bugs and (iteration + 1) < max_iterations:
                logger.info("[CrewAI] Critical bugs found, looping back to developer (iter %d)", iteration + 1)
                continue
            else:
                break

        # ── Reviewer ──────────────────────────────────────────────────────
        state = await self._run_reviewer(state, queue)
        return state

    # ── Developer ─────────────────────────────────────────────────────────────

    async def _run_developer(
        self,
        state: DevPipelineState,
        queue: asyncio.Queue[dict[str, Any]],
        iteration: int,
    ) -> DevPipelineState:
        plan = state["plan"]
        assert plan is not None

        await queue.put({"event": "agent_start", "agent": "developer", "data": {"iteration": iteration + 1}})

        runtime = state["runtime"]
        runtime_instructions = RUNTIME_INSTRUCTIONS.get(runtime, RUNTIME_INSTRUCTIONS["python"])

        # Build feedback section
        feedback_section = ""
        tr = state.get("test_report")
        if iteration > 0 and tr is not None:
            feedback_section = FEEDBACK_TEMPLATE.format(output_summary=tr.output_summary[:2000])
        if state.get("user_feedback"):
            feedback_section += USER_FEEDBACK_TEMPLATE.format(user_feedback=state["user_feedback"])

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
        for attempt in range(3):
            try:
                raw_response = await structured_llm.ainvoke(prompt)
                if isinstance(raw_response, dict):
                    if raw_response.get("parsed"):
                        result = raw_response["parsed"]
                        break
                    raw_text = raw_response.get("raw", "")
                else:
                    raw_text = raw_response
                if hasattr(raw_text, "content"):
                    raw_text = raw_text.content
                result = _repair_parse(str(raw_text))
                if result:
                    break
            except Exception as exc:
                logger.warning("[CrewAI] Developer parse attempt %d failed: %s", attempt + 1, exc)
                await asyncio.sleep(1)

        if result is None:
            raise RuntimeError("Failed to parse developer output after 3 attempts")

        files = {entry.filename: entry.content for entry in result.files}
        logger.info("[CrewAI] Developer generated %d files (iteration %d)", len(files), iteration + 1)

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

        state["files"] = files
        state["iteration"] = iteration + 1
        state["current_agent"] = "developer"
        return state

    # ── Tester ────────────────────────────────────────────────────────────────

    async def _run_tester(
        self,
        state: DevPipelineState,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> DevPipelineState:
        files = state["files"]
        runtime = state["runtime"]

        await queue.put({"event": "agent_start", "agent": "tester"})

        # Step 1: Sandbox tests
        sandbox_report = await run_tests_in_sandbox(files, runtime, queue)

        # Step 2: LLM static analysis
        files_summary = ""
        for fname, code in files.items():
            truncated = code[:4000] + ("..." if len(code) > 4000 else "")
            files_summary += f"\n--- {fname} ---\n{truncated}\n"

        static_prompt = STATIC_ANALYSIS_PROMPT.format(
            runtime=runtime,
            files_summary=files_summary[:20000],
        )

        try:
            llm = get_llm_for_role("tester", state.get("llm_context"))
            structured_llm = llm.with_structured_output(TestReport)
            llm_report: TestReport = await structured_llm.ainvoke(static_prompt)  # type: ignore[assignment]
        except Exception as exc:
            logger.warning("[CrewAI] LLM static analysis failed: %s", exc)
            llm_report = sandbox_report

        merged_report = TestReport(
            has_critical_bugs=sandbox_report.has_critical_bugs or llm_report.has_critical_bugs,
            passed_count=sandbox_report.passed_count,
            failed_count=sandbox_report.failed_count,
            error_count=sandbox_report.error_count,
            output_summary=sandbox_report.output_summary,
            test_cases=sandbox_report.test_cases,
            execution_time_ms=sandbox_report.execution_time_ms,
        )

        await queue.put({
            "event": "agent_output",
            "agent": "tester",
            "data": {
                "has_critical_bugs": merged_report.has_critical_bugs,
                "passed": merged_report.passed_count,
                "failed": merged_report.failed_count,
                "errors": merged_report.error_count,
                "summary": merged_report.output_summary[:300],
                "test_cases": [
                    {"name": tc.name, "passed": tc.passed, "error": tc.error_message[:100]}
                    for tc in merged_report.test_cases[:10]
                ],
            },
        })
        await queue.put({
            "event": "agent_complete",
            "agent": "tester",
            "data": {
                "has_critical_bugs": merged_report.has_critical_bugs,
                "passed": merged_report.passed_count,
                "failed": merged_report.failed_count,
            },
        })

        state["test_report"] = merged_report
        state["current_agent"] = "tester"
        return state

    # ── Reviewer ──────────────────────────────────────────────────────────────

    async def _run_reviewer(
        self,
        state: DevPipelineState,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> DevPipelineState:
        files = state["files"]
        plan = state["plan"]
        if plan is None:
            raise ValueError("Plan is missing")

        await queue.put({"event": "agent_start", "agent": "reviewer"})

        files_summary = ""
        for fname, code in files.items():
            files_summary += f"\n--- {fname} ---\n{code}\n"

        test_summary = "No tests run"
        tr = state.get("test_report")
        if tr is not None:
            test_summary = f"passed={tr.passed_count}, failed={tr.failed_count}, critical_bugs={tr.has_critical_bugs}"

        prompt = REVIEWER_PROMPT.format(
            app_name=plan.app_name,
            runtime=state["runtime"],
            test_summary=test_summary,
            files_summary=files_summary[:30000],
        )

        llm = get_llm_for_role("reviewer", state.get("llm_context"))
        structured_llm = llm.with_structured_output(_ReviewOutput)
        result: _ReviewOutput = await structured_llm.ainvoke(prompt)  # type: ignore[assignment]

        # Merge: original files + reviewer modifications
        merged_files = dict(files)
        merged_files.update(result.improved_files)

        logger.info("[CrewAI] Reviewer modified %d/%d files, %d notes",
                    len(result.improved_files), len(files), len(result.review_notes))

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

        state["files"] = merged_files
        state["review_notes"] = result.review_notes
        state["status"] = "done"
        state["current_agent"] = "reviewer"
        return state
