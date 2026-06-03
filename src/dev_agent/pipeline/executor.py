"""Multi-runtime sandbox executor — delegates to runtime-specific runners."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from src.dev_agent.pipeline.state import TestCase, TestReport
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


def get_runner_for_files(runtime: str, files: dict[str, str]) -> SandboxRunner:
    """Get the best runner by considering both the declared runtime AND the actual files.

    Prevents mis-routing when e.g. 'auto' leaks through or the planner picks
    'python' for a pure HTML/CSS/JS project.
    """
    # If we have an explicit match, use it
    if runtime in _RUNNERS:
        return _RUNNERS[runtime]()

    # Smart fallback: inspect the files to guess the best runner
    has_py = any(f.endswith(".py") for f in files)
    has_html = any(f.endswith(".html") for f in files)
    has_js_entry = any(f in ("server.js", "app.js", "index.js") for f in files)
    has_package_json = "package.json" in files

    if has_py:
        return PythonRunner()
    if has_js_entry or has_package_json:
        return NodeRunner()
    if has_html:
        return StaticRunner()

    # Ultimate fallback
    return PythonRunner()


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
            TestCase(
                name=str(tc.get("name", "")),
                passed=bool(tc.get("passed", False)),
                error_message=str(tc.get("error_message", ""))
            )
            for tc in sandbox_report.test_cases
        ],
        execution_time_ms=sandbox_report.execution_time_ms,
    )
