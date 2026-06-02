"""Pipeline state models — TypedDict for LangGraph + Pydantic for structured output."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel

Runtime = Literal["python", "node", "react", "angular", "static"]


# ── Pydantic models (used by with_structured_output) ──────────────────────────


class Task(BaseModel):
    id: str
    title: str
    description: str
    file_target: str


class Plan(BaseModel):
    idea: str
    app_name: str
    runtime: Runtime
    tech_stack: list[str]
    tasks: list[Task]
    architecture_notes: str
    estimated_files: list[str]
    entry_point: str = "main.py"


class TestCase(BaseModel):
    name: str
    passed: bool
    error_message: str = ""


class TestReport(BaseModel):
    has_critical_bugs: bool
    passed_count: int
    failed_count: int
    error_count: int
    output_summary: str
    test_cases: list[TestCase]
    execution_time_ms: int


class PreviewInfo(BaseModel):
    port: int
    url: str
    status: str = "stopped"
    pid: int | None = None


# ── LangGraph state (TypedDict — not serialized, no checkpointer) ─────────────


class DevPipelineState(TypedDict):
    session_id: str
    idea: str
    runtime: str
    plan: Plan | None
    files: dict[str, str]
    test_report: TestReport | None
    preview: PreviewInfo | None
    review_notes: list[str]
    iteration: int
    max_iterations: int
    status: str
    errors: list[str]
    current_agent: str
    event_queue: Any | None
    user_feedback: str
