"""Sandbox runner unit tests — no LLM, no API keys needed."""

import pytest

from src.dev_agent.sandbox.preview_server import _allocated_ports, allocate_port, release_port
from src.dev_agent.sandbox.python_runner import PythonRunner
from src.dev_agent.sandbox.static_runner import StaticRunner


@pytest.fixture(autouse=True)
def _clear_ports():
    """Reset port allocator state between tests."""
    _allocated_ports.clear()
    yield
    _allocated_ports.clear()


# ── Python Runner ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_python_runner_passes_valid_code() -> None:
    runner = PythonRunner()
    files = {
        "test_example.py": "def test_add():\n    assert 1 + 1 == 2\n",
    }
    report = runner.run_tests(files)
    assert report.has_critical_bugs is False
    assert report.passed_count >= 1
    assert report.failed_count == 0


@pytest.mark.unit
def test_python_runner_fails_bad_code() -> None:
    runner = PythonRunner()
    files = {
        "test_fail.py": "def test_bad():\n    assert 1 == 2\n",
    }
    report = runner.run_tests(files)
    assert report.has_critical_bugs is True
    assert report.failed_count >= 1


@pytest.mark.unit
def test_python_runner_no_tests() -> None:
    runner = PythonRunner()
    files = {
        "main.py": "print('hello')\n",
    }
    report = runner.run_tests(files)
    assert report.has_critical_bugs is False
    assert report.output_summary == "No test files found — skipping pytest"


# ── Static Runner ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_static_runner_with_index() -> None:
    runner = StaticRunner()
    files = {
        "index.html": "<html><body>Hello</body></html>",
        "style.css": "body { color: red; }",
    }
    report = runner.run_tests(files)
    assert report.has_critical_bugs is False
    assert report.passed_count == 1


@pytest.mark.unit
def test_static_runner_no_index() -> None:
    runner = StaticRunner()
    files = {
        "app.js": "console.log('hi');",
    }
    report = runner.run_tests(files)
    assert report.has_critical_bugs is True
    assert report.failed_count == 1


# ── Port Allocator ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_allocate_port_unique() -> None:
    p1 = allocate_port("session-1")
    p2 = allocate_port("session-2")
    p3 = allocate_port("session-3")
    assert len({p1, p2, p3}) == 3
    assert all(9100 <= p <= 9120 for p in [p1, p2, p3])


@pytest.mark.unit
def test_allocate_port_reuses_same_session() -> None:
    p1 = allocate_port("session-1")
    p2 = allocate_port("session-1")
    assert p1 == p2


@pytest.mark.unit
def test_allocate_port_exhaustion() -> None:
    for i in range(21):
        allocate_port(f"session-{i}")
    with pytest.raises(RuntimeError, match="No preview ports available"):
        allocate_port("session-overflow")


@pytest.mark.unit
def test_release_port_frees_slot() -> None:
    for i in range(21):
        allocate_port(f"session-{i}")
    release_port("session-0")
    # Should now be able to allocate again
    p = allocate_port("session-new")
    assert 9100 <= p <= 9120
