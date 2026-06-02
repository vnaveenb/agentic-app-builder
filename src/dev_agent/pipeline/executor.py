"""Multi-runtime sandbox executor — delegates to runtime-specific runners."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from src.dev_agent.pipeline.state import TestReport
from src.dev_agent.sandbox.base import SandboxRunner
from src.dev_agent.sandbox.base import TestReport as SandboxTestReport
from src.dev_agent.sandbox.node_runner import NodeRunner
from src.dev_agent.sandbox.python_runner import PythonRunner
from src.dev_agent.sandbox.static_runner import StaticRunner

_RUNNERS: dict[str, type[SandboxRunner]] = {
    "python": PythonRunner,
    "node": NodeRunner,
    "react": NodeRunner,
    "angular": NodeRunner,
    "static": StaticRunner,
}


def get_runner(runtime: str) -> SandboxRunner:
    """Get the appropriate sandbox runner for a runtime."""
    runner_cls = _RUNNERS.get(runtime, PythonRunner)
    return runner_cls()


async def run_tests_in_sandbox(
    files: dict[str, str],
    runtime: str,
    event_queue: asyncio.Queue[dict[str, Any]] | None = None,
) -> TestReport:
    """Run tests using the appropriate sandbox runner. Non-blocking (thread executor)."""
    runner = get_runner(runtime)
    loop = asyncio.get_event_loop()
    sandbox_report: SandboxTestReport = await loop.run_in_executor(
        None, partial(runner.run_tests, files, event_queue)
    )
    # Convert sandbox TestReport dataclass to pipeline Pydantic TestReport
    return TestReport(
        has_critical_bugs=sandbox_report.has_critical_bugs,
        passed_count=sandbox_report.passed_count,
        failed_count=sandbox_report.failed_count,
        error_count=sandbox_report.error_count,
        output_summary=sandbox_report.output_summary,
        test_cases=[
            {"name": tc.get("name", ""), "passed": tc.get("passed", False), "error_message": tc.get("error_message", "")}
            for tc in sandbox_report.test_cases
        ],
        execution_time_ms=sandbox_report.execution_time_ms,
    )
