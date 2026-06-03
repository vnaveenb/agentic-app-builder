"""Mocked agent unit tests — no LLM calls, no API keys needed."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.dev_agent.pipeline.state import DevPipelineState, Plan, Task, TestReport


def _make_state(**overrides: object) -> DevPipelineState:
    """Create a minimal valid DevPipelineState for testing."""
    base: DevPipelineState = {
        "session_id": "test-session",
        "idea": "Build a todo REST API",
        "runtime": "python",
        "plan": None,
        "files": {},
        "test_report": None,
        "preview": None,
        "review_notes": [],
        "iteration": 0,
        "max_iterations": 2,
        "status": "running",
        "errors": [],
        "current_agent": "",
        "event_queue": None,
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


def _make_plan() -> Plan:
    return Plan(
        idea="Build a todo REST API",
        app_name="todo_api",
        runtime="python",
        tech_stack=["flask", "python"],
        tasks=[Task(id="1", title="Create app", description="Build Flask app", file_target="app.py")],
        architecture_notes="Simple Flask REST API",
        estimated_files=["app.py", "test_app.py", "requirements.txt"],
        entry_point="app.py",
    )


# ── Planner Tests ─────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_planner_returns_plan() -> None:
    plan = _make_plan()
    mock_llm = MagicMock()
    mock_structured = MagicMock()
    mock_structured.ainvoke = AsyncMock(return_value=plan)
    mock_llm.with_structured_output.return_value = mock_structured

    with patch("src.dev_agent.agents.planner.get_llm", return_value=mock_llm):
        from src.dev_agent.agents.planner import planner_node

        state = _make_state()
        result = await planner_node(state)

    assert result["plan"] == plan
    assert result["runtime"] == "python"
    assert result["current_agent"] == "planner"


# ── Developer Tests ───────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_developer_increments_iteration() -> None:
    from pydantic import BaseModel

    class _FakeOutput(BaseModel):
        files: list[dict[str, str]]
        implementation_notes: str

    # Create a mock that returns a proper _DeveloperOutput-like object
    mock_llm = MagicMock()
    mock_structured = MagicMock()

    # The developer uses _DeveloperOutput internally, mock accordingly
    fake_parsed = MagicMock()
    fake_parsed.files = [MagicMock(filename="app.py", content="print('hello')")]
    fake_parsed.implementation_notes = "Done"
    # Because include_raw=True, the mock should return a dict
    fake_result = {"parsed": fake_parsed, "raw": ""}
    mock_structured.ainvoke = AsyncMock(return_value=fake_result)
    mock_llm.with_structured_output.return_value = mock_structured

    with patch("src.dev_agent.agents.developer.get_llm", return_value=mock_llm):
        from src.dev_agent.agents.developer import developer_node

        state = _make_state(plan=_make_plan(), iteration=0)
        result = await developer_node(state)

    assert result["iteration"] == 1
    assert "app.py" in result["files"]
    assert result["current_agent"] == "developer"


# ── Tester Tests ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tester_merges_reports() -> None:
    # Mock sandbox to return passing results
    sandbox_report = TestReport(
        has_critical_bugs=False,
        passed_count=2,
        failed_count=0,
        error_count=0,
        output_summary="2 passed",
        test_cases=[],
        execution_time_ms=100,
    )

    # Mock LLM to flag a critical bug
    llm_report = TestReport(
        has_critical_bugs=True,
        passed_count=0,
        failed_count=0,
        error_count=0,
        output_summary="Security issue found",
        test_cases=[],
        execution_time_ms=0,
    )

    mock_llm = MagicMock()
    mock_structured = MagicMock()
    mock_structured.ainvoke = AsyncMock(return_value=llm_report)
    mock_llm.with_structured_output.return_value = mock_structured

    with (
        patch("src.dev_agent.agents.tester.run_tests_in_sandbox", new_callable=AsyncMock, return_value=sandbox_report),
        patch("src.dev_agent.agents.tester.get_llm", return_value=mock_llm),
    ):
        from src.dev_agent.agents.tester import tester_node

        state = _make_state(
            files={"app.py": "print('hello')", "test_app.py": "def test_x(): pass"},
            runtime="python",
        )
        result = await tester_node(state)

    report = result["test_report"]
    # Merged: sandbox passed but LLM found critical bug → has_critical_bugs=True
    assert report.has_critical_bugs is True
    # Sandbox counts are authoritative
    assert report.passed_count == 2
    assert report.failed_count == 0


# ── Reviewer Tests ────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reviewer_sets_status_done() -> None:
    mock_llm = MagicMock()
    mock_structured = MagicMock()
    fake_result = MagicMock()
    fake_result.improved_files = {"app.py": "# improved\nprint('hello')"}
    fake_result.review_notes = ["Good structure", "Consider adding logging"]
    mock_structured.ainvoke = AsyncMock(return_value=fake_result)
    mock_llm.with_structured_output.return_value = mock_structured

    with patch("src.dev_agent.agents.reviewer.get_llm", return_value=mock_llm):
        from src.dev_agent.agents.reviewer import reviewer_node

        state = _make_state(
            plan=_make_plan(),
            files={"app.py": "print('hello')"},
            runtime="python",
        )
        result = await reviewer_node(state)

    assert result["status"] == "done"
    assert "app.py" in result["files"]
    assert len(result["review_notes"]) == 2
    assert result["current_agent"] == "reviewer"
