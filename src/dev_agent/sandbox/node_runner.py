"""Node.js sandbox runner — handles Node, React (CDN), Angular (CDN) projects."""

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


class NodeRunner(SandboxRunner):
    """Runs Node.js tests and previews for Node/React/Angular projects."""

    def run_tests(self, files: dict[str, str], event_queue: asyncio.Queue | None = None) -> TestReport:
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="dev_agent_node_") as tmpdir:
            tmp = pathlib.Path(tmpdir)
            for name, content in files.items():
                path = tmp / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            # If package.json exists, install deps (limited timeout)
            if (tmp / "package.json").exists():
                emit_terminal(event_queue, "tester", "$ npm install --omit=dev")
                try:
                    install_result = subprocess.run(
                        ["npm", "install", "--omit=dev"],
                        cwd=tmpdir,
                        timeout=60,
                        capture_output=True,
                        text=True,
                    )
                    if install_result.stdout.strip():
                        emit_terminal(event_queue, "install", install_result.stdout)
                    if install_result.stderr.strip():
                        emit_terminal(event_queue, "install", install_result.stderr)
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass  # Continue without deps — CDN apps don't need them

            # Find test file
            test_file = None
            for candidate in ["test.js", "test.mjs", "tests/test.js"]:
                if (tmp / candidate).exists():
                    test_file = candidate
                    break

            if not test_file:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                msg = "No test files found — skipping"
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

            # Use Node 20 built-in test runner
            cmd = ["node", "--test", test_file]
            emit_terminal(event_queue, "tester", f"$ node --test {test_file}")
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=tmpdir,
                )
                output = result.stdout + result.stderr
                return_code = result.returncode
                emit_terminal(event_queue, "tester", output)
            except subprocess.TimeoutExpired:
                msg = "Node execution timed out"
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
            except FileNotFoundError:
                msg = "Node.js not found in PATH"
                emit_terminal(event_queue, "tester", f"ERROR: {msg}")
                return TestReport(
                    has_critical_bugs=True,
                    passed_count=0,
                    failed_count=0,
                    error_count=1,
                    output_summary=msg,
                    test_cases=[],
                    execution_time_ms=0,
                )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return _parse_node_output(output, return_code, elapsed_ms)

    def start_preview(self, files: dict[str, str], port: int) -> int:
        """Start Node.js server or serve static React/Angular files."""
        tmpdir = tempfile.mkdtemp(prefix="dev_agent_preview_node_")
        tmp = pathlib.Path(tmpdir)
        for name, content in files.items():
            path = tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        # Log stderr for debugging
        stderr_log = tmp / "_preview_stderr.log"
        stderr_fh = stderr_log.open("w", encoding="utf-8")

        # If has server.js/index.js/app.js → run via shim; else serve as static
        entry = next(
            (f for f in ["server.js", "index.js", "app.js"] if (tmp / f).exists()),
            None,
        )
        if entry:
            # Write a shim that patches net.Server.prototype.listen to always
            # use the allocated port, regardless of what the generated code
            # hardcodes.  Then require the user entry so it starts up.
            shim = tmp / "_preview_shim.js"
            shim.write_text(
                f'process.env.PORT = "{port}";\n'
                f'const net = require("net");\n'
                f'const _origListen = net.Server.prototype.listen;\n'
                f'net.Server.prototype.listen = function(...args) {{\n'
                f'  // Replace port arg with allocated preview port\n'
                f'  if (typeof args[0] === "number" || (typeof args[0] === "string" && /^\\d+$/.test(args[0]))) {{\n'
                f'    args[0] = {port};\n'
                f'  }} else if (args[0] && typeof args[0] === "object" && "port" in args[0]) {{\n'
                f'    args[0] = {{ ...args[0], port: {port}, host: "0.0.0.0" }};\n'
                f'  }}\n'
                f'  return _origListen.apply(this, args);\n'
                f'}};\n'
                f'const app = require("./{entry}");\n'
                f'// If the entry exports an unbound app, start it\n'
                f'if (app && typeof app.listen === "function" && !app.listening) {{\n'
                f'  app.listen({port}, "0.0.0.0");\n'
                f'}}\n',
                encoding="utf-8",
            )
            proc = subprocess.Popen(
                ["node", "_preview_shim.js"],
                cwd=tmpdir,
                env={**os.environ, "PORT": str(port)},
                stdout=subprocess.DEVNULL,
                stderr=stderr_fh,
            )
        else:
            # Serve as static — use Python http.server as fallback
            proc = subprocess.Popen(
                ["python", "-m", "http.server", str(port), "--bind", "0.0.0.0"],
                cwd=tmpdir,
                stdout=subprocess.DEVNULL,
                stderr=stderr_fh,
            )
        return proc.pid

    def stop_preview(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _parse_node_output(output: str, return_code: int, elapsed_ms: int) -> TestReport:
    """Parse Node 20 TAP-style test output."""
    passed = 0
    failed = 0
    test_cases: list[dict[str, object]] = []

    for line in output.splitlines():
        # Node 20 TAP: "ok 1 - test name" / "not ok 2 - test name"
        ok_match = re.match(r"ok\s+\d+\s*-?\s*(.*)", line)
        not_ok_match = re.match(r"not ok\s+\d+\s*-?\s*(.*)", line)
        if ok_match:
            passed += 1
            test_cases.append({"name": ok_match.group(1).strip(), "passed": True, "error_message": ""})
        elif not_ok_match:
            failed += 1
            test_cases.append({"name": not_ok_match.group(1).strip(), "passed": False, "error_message": ""})

    # Also check for "# pass N" / "# fail N" summary
    pass_match = re.search(r"# pass\s+(\d+)", output)
    fail_match = re.search(r"# fail\s+(\d+)", output)
    if pass_match:
        passed = max(passed, int(pass_match.group(1)))
    if fail_match:
        failed = max(failed, int(fail_match.group(1)))

    has_critical_bugs = return_code != 0
    output_summary = output[:3000] if len(output) > 3000 else output

    return TestReport(
        has_critical_bugs=has_critical_bugs,
        passed_count=passed,
        failed_count=failed,
        error_count=0,
        output_summary=output_summary,
        test_cases=test_cases,
        execution_time_ms=elapsed_ms,
    )
