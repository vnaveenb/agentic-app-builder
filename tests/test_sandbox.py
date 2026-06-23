"""Sandbox runner unit tests — no LLM, no API keys needed."""

import asyncio

import pytest

from src.dev_agent.sandbox import preview_server
from src.dev_agent.sandbox.preview_server import (
    _allocated_ports,
    allocate_port,
    get_preview_mode,
    is_static_preview,
    release_port,
    resolve_static_file,
)
from src.dev_agent.sandbox.python_runner import PythonRunner, _strip_server_startup
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


# ── Tiered preview classification ─────────────────────────────────────────────


@pytest.mark.unit
def test_is_static_preview_pure_frontend() -> None:
    files = {"index.html": "<html></html>", "app.js": "console.log(1)", "s.css": "body{}"}
    assert is_static_preview("static", files) is True
    assert is_static_preview("react", files) is True
    assert is_static_preview("angular", files) is True


@pytest.mark.unit
def test_is_static_preview_flask_is_server() -> None:
    files = {"app.py": "from flask import Flask\napp = Flask(__name__)", "index.html": "x"}
    assert is_static_preview("python", files) is False


@pytest.mark.unit
def test_is_static_preview_fastapi_is_server() -> None:
    files = {"main.py": "from fastapi import FastAPI\napp = FastAPI()"}
    assert is_static_preview("python", files) is False


@pytest.mark.unit
def test_is_static_preview_node_server() -> None:
    files = {"server.js": "require('http').createServer()", "index.html": "x"}
    assert is_static_preview("node", files) is False


@pytest.mark.unit
def test_is_static_preview_no_index() -> None:
    assert is_static_preview("static", {"app.js": "console.log(1)"}) is False


# ── Static preview serves directly, with no subprocess or port ────────────────


@pytest.mark.unit
def test_start_preview_static_uses_no_subprocess_or_port() -> None:
    sid = "static-sess"
    files = {"index.html": "<html><body>Hi</body></html>", "style.css": "body{color:red}"}
    runner = StaticRunner()
    try:
        info = asyncio.run(preview_server.start_preview(sid, files, runner, "static"))
        assert info["mode"] == "static"
        assert info["status"] == "running"
        # Tiered static path must NOT allocate a port or spawn a process.
        assert sid not in preview_server._allocated_ports
        assert sid not in preview_server._active_pids
        assert get_preview_mode(sid) == "static"

        # Root path resolves to index.html and is served from the session dir.
        target = resolve_static_file(sid, "")
        assert target is not None and target.name == "index.html"
        assert resolve_static_file(sid, "style.css") is not None
        # Path traversal is blocked.
        assert resolve_static_file(sid, "../../../../etc/passwd") is None
    finally:
        preview_server.stop_preview(sid, runner)
        assert get_preview_mode(sid) is None  # cleanup clears mode


# ── Flask port enforcement: generated startup is neutralized ──────────────────


@pytest.mark.unit
def test_strip_server_startup_removes_hardcoded_flask_port() -> None:
    """Guards the preview bug fix: a hardcoded app.run(port=...) must be stripped
    so the Flask CLI (with the allocated --port) controls the bind port."""
    source = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.route('/')\n"
        "def home():\n"
        "    return 'hi'\n"
        "if __name__ == '__main__':\n"
        "    app.run(host='0.0.0.0', port=5000)\n"
    )
    stripped = _strip_server_startup(source)
    assert "app.run" not in stripped
    assert "__main__" not in stripped
    assert "@app.route('/')" in stripped  # the app itself is preserved
