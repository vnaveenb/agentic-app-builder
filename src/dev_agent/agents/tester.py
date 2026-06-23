"""Tester agent — runs sandbox tests + LLM static analysis."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from shared.providers import get_llm
from src.dev_agent.agents.retry import TESTER_TASKS, emit_task_done, emit_tasks, retry_llm_call
from src.dev_agent.pipeline.executor import run_tests_in_sandbox
from src.dev_agent.pipeline.state import DevPipelineState, TestReport

logger = logging.getLogger(__name__)

_STATIC_ANALYSIS_PROMPT = """\
You are a senior code reviewer. Analyze the following code for critical bugs, security issues, and logic errors.

Runtime: {runtime}
Files:
{files_summary}

Evaluate:
1. Are there any critical bugs that would prevent the app from running?
2. Are there security vulnerabilities (SQL injection, XSS, path traversal, etc.)?
3. Are there logic errors in the core functionality?
4. Does the entry point work correctly?

Return a test report with your findings. Set has_critical_bugs=true ONLY if there are bugs that would crash the app or create severe security holes. Minor style issues are NOT critical.
"""


async def tester_node(state: DevPipelineState) -> dict[str, Any]:
    """LangGraph node: test generated code via sandbox + LLM analysis."""
    queue: asyncio.Queue | None = state.get("event_queue")
    files = state["files"]
    runtime = state["runtime"]

    if queue:
        await queue.put({"event": "agent_start", "agent": "tester"})
        await emit_tasks(queue, "tester", TESTER_TASKS)

    # Step 1: Run actual tests in sandbox
    sandbox_report = await run_tests_in_sandbox(files, runtime, queue)

    await emit_task_done(queue, "tester", 0)  # Running sandbox tests

    logger.info(
        "Sandbox results: passed=%d failed=%d critical=%s",
        sandbox_report.passed_count,
        sandbox_report.failed_count,
        sandbox_report.has_critical_bugs,
    )

    # Step 2: LLM static analysis (truncate code to 4000 chars/file)
    files_summary = ""
    for fname, code in files.items():
        truncated = code[:4000] + ("..." if len(code) > 4000 else "")
        files_summary += f"\n--- {fname} ---\n{truncated}\n"

    prompt = _STATIC_ANALYSIS_PROMPT.format(
        runtime=runtime,
        files_summary=files_summary[:20000],  # Overall limit
    )

    try:
        llm = get_llm(temperature=0.0)
        structured_llm = llm.with_structured_output(TestReport)
        llm_report: TestReport = await retry_llm_call(
            structured_llm.ainvoke,
            prompt,
            agent_name="tester",
            queue=queue,
            task_id=1,
            task_text="Performing static analysis",
        )  # type: ignore[assignment]
    except Exception as exc:
        logger.warning("LLM static analysis failed: %s", exc)
        # Fall back to sandbox-only results
        llm_report = sandbox_report

    await emit_task_done(queue, "tester", 1)  # Performing static analysis

    # Merge: sandbox counts are authoritative
    merged_report = TestReport(
        has_critical_bugs=sandbox_report.has_critical_bugs or llm_report.has_critical_bugs,
        passed_count=sandbox_report.passed_count,
        failed_count=sandbox_report.failed_count,
        error_count=sandbox_report.error_count,
        output_summary=sandbox_report.output_summary,
        test_cases=sandbox_report.test_cases,
        execution_time_ms=sandbox_report.execution_time_ms,
    )

    await emit_task_done(queue, "tester", 2)  # Merging results

    if queue:
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

    return {
        "test_report": merged_report,
        "current_agent": "tester",
    }
