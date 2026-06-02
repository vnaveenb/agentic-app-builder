"""Abstract base for sandbox runners."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TestReport:
    """Lightweight test result used by sandbox runners (mirrors pipeline state model)."""

    has_critical_bugs: bool
    passed_count: int
    failed_count: int
    error_count: int
    output_summary: str
    test_cases: list[dict[str, object]]
    execution_time_ms: int


def emit_terminal(queue: asyncio.Queue | None, source: str, text: str) -> None:
    """Push a terminal event to the session queue (non-blocking, fire-and-forget)."""
    if queue is None or not text.strip():
        return
    try:
        queue.put_nowait({"event": "terminal", "data": {"source": source, "text": text}})
    except asyncio.QueueFull:
        pass


class SandboxRunner(ABC):
    """Base class for all runtime-specific sandbox runners."""

    timeout: int = 30

    @abstractmethod
    def run_tests(self, files: dict[str, str], event_queue: asyncio.Queue | None = None) -> TestReport:
        """Execute tests for generated files. Blocking — called in thread executor."""
        ...

    @abstractmethod
    def start_preview(self, files: dict[str, str], port: int) -> int:
        """Start a preview server on given port. Returns PID. Blocking."""
        ...

    @abstractmethod
    def stop_preview(self, pid: int) -> None:
        """Kill preview server by PID."""
        ...
