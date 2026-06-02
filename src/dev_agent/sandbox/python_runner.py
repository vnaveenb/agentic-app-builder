"""Python sandbox runner — pytest execution + FastAPI/Flask preview."""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import re
import signal
import subprocess
import tempfile
import time

from src.dev_agent.sandbox.base import SandboxRunner, TestReport, emit_terminal

logger = logging.getLogger(__name__)


def _strip_server_startup(source: str) -> str:
    """Remove server-start boilerplate so uvicorn/flask CLI can control the port.

    Strips:
      - ``if __name__ == "__main__":`` blocks (and everything indented beneath)
      - Module-level ``uvicorn.run(...)`` calls
      - Module-level ``app.run(...)`` / ``application.run(...)`` calls
    """
    # 1) Remove `if __name__ == "__main__":` block (possibly multi-line body)
    source = re.sub(
        r'^if\s+__name__\s*==\s*["\']__main__["\']\s*:.*?(?=\n\S|\Z)',
        '',
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    # 2) Remove module-level uvicorn.run(...) — handles multi-line via greedy paren match
    source = re.sub(
        r'^uvicorn\.run\(.*?\)\s*$',
        '',
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    # 3) Remove module-level app.run(...) / application.run(...)
    source = re.sub(
        r'^(?:app|application)\.run\(.*?\)\s*$',
        '',
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    return source


def _ensure_flask_root_route(source: str, files: dict[str, str], tmp: pathlib.Path) -> str:
    """If the Flask app lacks a route for '/', inject one that serves index.html."""
    # Check if there's already a route for '/'
    if re.search(r"""@app\.route\(\s*['"]\/['"]""", source):
        return source

    # Look for an index.html in the generated files
    index_content: str | None = None
    for fname in ("index.html", "templates/index.html", "static/index.html"):
        if fname in files:
            index_content = files[fname]
            break

    if not index_content:
        # No index.html, inject a simple fallback route
        inject = """
@app.route('/')
def _fallback_index():
    return '<h1>App is running</h1><p>No root route defined. Check /api endpoints.</p>'
"""
    else:
        # Write index.html to tmpdir root and serve it
        (tmp / "_index_fallback.html").write_text(index_content, encoding="utf-8")
        inject = """
import os as _os
@app.route('/')
def _fallback_index():
    _path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '_index_fallback.html')
    with open(_path, encoding='utf-8') as _f:
        return _f.read()
"""

    # Inject just before if __name__ or at the end
    main_match = re.search(r"^if\s+__name__\s*==", source, re.MULTILINE)
    if main_match:
        source = source[:main_match.start()] + inject + "\n" + source[main_match.start():]
    else:
        source += "\n" + inject
    logger.info("Injected fallback root route for Flask app")
    return source


class PythonRunner(SandboxRunner):
    """Runs Python tests via pytest and previews via direct script execution."""

    def run_tests(self, files: dict[str, str], event_queue: asyncio.Queue | None = None) -> TestReport:
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="dev_agent_py_") as tmpdir:
            tmp = pathlib.Path(tmpdir)
            for name, content in files.items():
                path = tmp / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            # Find test files
            test_files = [f for f in files if f.startswith("test") and f.endswith(".py")]
            if not test_files:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                msg = "No test files found — skipping pytest"
                emit_terminal(event_queue, "tester", msg)
                return TestReport(
                    has_critical_bugs=False,
                    passed_count=0,
                    failed_count=0,
                    error_count=0,
                    output_summary=msg,
                    test_cases=[],
                    execution_time_ms=elapsed_ms,
                )

            emit_terminal(event_queue, "tester", f"$ pytest {' '.join(test_files)}")
            try:
                result = subprocess.run(
                    ["python", "-m", "pytest", str(tmp), "-v", "--tb=short", "-q", "--no-header"],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=tmpdir,
                )
                output = result.stdout + result.stderr
                return_code = result.returncode
                emit_terminal(event_queue, "tester", output)
            except subprocess.TimeoutExpired:
                msg = "Execution timed out"
                emit_terminal(event_queue, "tester", f"ERROR: {msg}")
                return TestReport(
                    has_critical_bugs=True,
                    passed_count=0,
                    failed_count=1,
                    error_count=1,
                    output_summary=msg,
                    test_cases=[],
                    execution_time_ms=self.timeout * 1000,
                )
            except Exception as exc:
                emit_terminal(event_queue, "tester", f"ERROR: {exc}")
                return TestReport(
                    has_critical_bugs=True,
                    passed_count=0,
                    failed_count=1,
                    error_count=1,
                    output_summary=str(exc),
                    test_cases=[],
                    execution_time_ms=0,
                )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return _parse_pytest_output(output, return_code, elapsed_ms)

    def start_preview(self, files: dict[str, str], port: int) -> int:
        """Start a Python app (FastAPI/Flask) on the given port."""
        tmpdir = tempfile.mkdtemp(prefix="dev_agent_preview_py_")
        tmp = pathlib.Path(tmpdir)
        for name, content in files.items():
            path = tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        # Detect entry point and framework from file contents
        entry_stem = "main" if (tmp / "main.py").exists() else "app"
        entry_path = tmp / f"{entry_stem}.py"
        source = entry_path.read_text(encoding="utf-8") if entry_path.exists() else ""

        env = {**os.environ, "PORT": str(port)}

        # Log stderr to file for debugging preview failures
        stderr_log = tmp / "_preview_stderr.log"
        stderr_fh = stderr_log.open("w", encoding="utf-8")

        if "FastAPI" in source or "Starlette" in source:
            # Strip server-start calls so uvicorn CLI controls the port
            sanitized = _strip_server_startup(source)
            if sanitized != source:
                entry_path.write_text(sanitized, encoding="utf-8")
                logger.info("Stripped server-start boilerplate from %s", entry_stem)
            # Use uvicorn CLI — imports module without executing __main__ guards
            cmd = [
                "python", "-m", "uvicorn",
                f"{entry_stem}:app",
                "--host", "0.0.0.0",
                "--port", str(port),
                "--app-dir", tmpdir,
                "--log-level", "warning",
            ]
        elif "Flask" in source or "flask" in source:
            # Inject a fallback root route if the app doesn't already serve /
            # and there's an index.html available in the generated files
            source = _ensure_flask_root_route(source, files, tmp)
            entry_path.write_text(source, encoding="utf-8")
            cmd = ["python", f"{entry_stem}.py"]
        else:
            # Generic fallback: serve as static if index.html exists,
            # otherwise run the script directly with PORT injected
            if (tmp / "index.html").exists():
                cmd = ["python", "-m", "http.server", str(port), "--bind", "0.0.0.0"]
            else:
                cmd = ["python", f"{entry_stem}.py"]

        proc = subprocess.Popen(
            cmd,
            cwd=tmpdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Store stderr pipe for async reading by preview_server
        proc._stderr_log_path = str(stderr_log)  # type: ignore[attr-defined]
        # Write initial stderr to log file for backward compat
        self._start_stderr_logger(proc, stderr_log)
        return proc.pid

    @staticmethod
    def _start_stderr_logger(proc: subprocess.Popen, log_path: pathlib.Path) -> None:
        """Background thread to drain stderr to log file."""
        import threading

        def _drain() -> None:
            try:
                with log_path.open("w", encoding="utf-8") as fh:
                    for line in proc.stderr:  # type: ignore[union-attr]
                        if isinstance(line, bytes):
                            line = line.decode("utf-8", errors="replace")
                        fh.write(line)
                        fh.flush()
            except (OSError, ValueError):
                pass

        t = threading.Thread(target=_drain, daemon=True)
        t.start()

    def stop_preview(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _parse_pytest_output(output: str, return_code: int, elapsed_ms: int) -> TestReport:
    """Parse pytest -v output into a TestReport."""
    passed = 0
    failed = 0
    errors = 0
    test_cases: list[dict[str, object]] = []

    # Parse summary line: "3 passed, 1 failed, 1 error"
    summary_match = re.search(
        r"(\d+)\s+passed(?:.*?(\d+)\s+failed)?(?:.*?(\d+)\s+error)?", output
    )
    if summary_match:
        passed = int(summary_match.group(1))
        failed = int(summary_match.group(2) or 0)
        errors = int(summary_match.group(3) or 0)
    else:
        # Try alternative patterns
        passed_m = re.search(r"(\d+) passed", output)
        failed_m = re.search(r"(\d+) failed", output)
        error_m = re.search(r"(\d+) error", output)
        if passed_m:
            passed = int(passed_m.group(1))
        if failed_m:
            failed = int(failed_m.group(1))
        if error_m:
            errors = int(error_m.group(1))

    # Parse individual test results from verbose output
    for line in output.splitlines():
        if " PASSED" in line:
            name = line.split(" PASSED")[0].strip()
            test_cases.append({"name": name, "passed": True, "error_message": ""})
        elif " FAILED" in line:
            name = line.split(" FAILED")[0].strip()
            test_cases.append({"name": name, "passed": False, "error_message": ""})

    has_critical_bugs = return_code != 0
    # Truncate output for storage
    output_summary = output[:3000] if len(output) > 3000 else output

    return TestReport(
        has_critical_bugs=has_critical_bugs,
        passed_count=passed,
        failed_count=failed,
        error_count=errors,
        output_summary=output_summary,
        test_cases=test_cases,
        execution_time_ms=elapsed_ms,
    )
