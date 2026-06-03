"""Static file sandbox runner — handles pure HTML/CSS/JS projects."""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import signal
import subprocess
import tempfile
import time

from src.dev_agent.sandbox.base import PreviewInfo, SandboxRunner, TestReport, emit_terminal

logger = logging.getLogger(__name__)


class StaticRunner(SandboxRunner):
    """Handles pure HTML/CSS/JS projects — no build step needed."""

    def run_tests(self, files: dict[str, str], event_queue: asyncio.Queue | None = None) -> TestReport:
        """Validate that index.html exists (static files can't be unit-tested server-side)."""
        start = time.monotonic()
        has_index = any("index.html" in f for f in files)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if not has_index:
            msg = "No index.html found — static projects require an entry point"
            emit_terminal(event_queue, "tester", f"FAIL: {msg}")
            return TestReport(
                has_critical_bugs=True,
                passed_count=0,
                failed_count=1,
                error_count=0,
                output_summary=msg,
                test_cases=[{"name": "index.html exists", "passed": False, "error_message": "Missing index.html"}],
                execution_time_ms=elapsed_ms,
            )

        msg = "Static files validated — index.html present"
        emit_terminal(event_queue, "tester", f"PASS: {msg}")
        return TestReport(
            has_critical_bugs=False,
            passed_count=1,
            failed_count=0,
            error_count=0,
            output_summary=msg,
            test_cases=[{"name": "index.html exists", "passed": True, "error_message": ""}],
            execution_time_ms=elapsed_ms,
        )

    def start_preview(self, files: dict[str, str], port: int) -> PreviewInfo:
        """Serve static files using Python's built-in HTTP server."""
        tmpdir = tempfile.mkdtemp(prefix="dev_agent_preview_static_")
        tmp = pathlib.Path(tmpdir)
        for name, content in files.items():
            path = tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        # If index.html is nested (e.g. src/index.html, public/index.html)
        # but not at root, copy it to root so http.server can find it
        if not (tmp / "index.html").exists():
            for name in files:
                if name.endswith("index.html") and name != "index.html":
                    (tmp / "index.html").write_text(files[name], encoding="utf-8")
                    logger.info("Copied nested %s to tmpdir root", name)
                    break

        logger.info("StaticRunner preview: port=%d, files=%s", port, list(files.keys()))

        # Log stderr for debugging; stdout to DEVNULL to prevent pipe stalls
        stderr_log = tmp / "_preview_stderr.log"
        stderr_fh = stderr_log.open("w", encoding="utf-8")

        proc = subprocess.Popen(
            ["python", "-m", "http.server", str(port), "--bind", "0.0.0.0"],
            cwd=tmpdir,
            stdout=subprocess.DEVNULL,
            stderr=stderr_fh,
        )
        return PreviewInfo(pid=proc.pid, tmpdir=tmpdir)

    def stop_preview(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
