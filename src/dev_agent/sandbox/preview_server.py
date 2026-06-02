"""Preview lifecycle manager — allocates ports, tracks PIDs, handles cleanup."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import socket
import time

from src.dev_agent.sandbox.base import SandboxRunner

logger = logging.getLogger(__name__)

_PORT_RANGE = range(9100, 9121)  # 20 concurrent previews max
_allocated_ports: dict[str, int] = {}  # session_id → port
_active_pids: dict[str, int] = {}  # session_id → PID
_preview_dirs: dict[str, str] = {}  # session_id → tmpdir path


def allocate_port(session_id: str) -> int:
    """Allocate a unique preview port for a session. Raises RuntimeError if exhausted."""
    # If session already has a port, reuse it
    if session_id in _allocated_ports:
        return _allocated_ports[session_id]

    used = set(_allocated_ports.values())
    for port in _PORT_RANGE:
        if port not in used:
            _allocated_ports[session_id] = port
            return port
    raise RuntimeError("No preview ports available — all 20 slots in use")


def release_port(session_id: str) -> None:
    """Release a port back to the pool."""
    _allocated_ports.pop(session_id, None)


async def _wait_for_port(port: int, timeout: float = 8.0) -> bool:
    """Poll until a port is accepting TCP connections (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.25)
    return False


async def start_preview(
    session_id: str,
    files: dict[str, str],
    runner: SandboxRunner,
) -> dict[str, object]:
    """Start a preview server for the session. Returns preview metadata."""
    port = allocate_port(session_id)
    loop = asyncio.get_event_loop()
    pid = await loop.run_in_executor(None, runner.start_preview, files, port)
    _active_pids[session_id] = pid

    # Wait for the server to actually bind the port before returning success
    ready = await _wait_for_port(port, timeout=12.0)
    if not ready:
        # Check if process is still alive
        try:
            os.kill(pid, 0)  # signal 0 = existence check
        except OSError:
            _active_pids.pop(session_id, None)
            release_port(session_id)
            # Try to read stderr log for diagnostics
            stderr_msg = _read_preview_stderr(pid)
            detail = f"Preview process exited before binding port {port}"
            if stderr_msg:
                detail += f": {stderr_msg}"
            raise RuntimeError(detail) from None
        logger.warning("Preview on port %d not ready after 12s — proceeding", port)

    return {
        "port": port,
        "url": f"/preview/{session_id}",
        "status": "running",
        "pid": pid,
    }


async def stream_preview_stderr(
    session_id: str,
    queue: asyncio.Queue | None,
) -> None:
    """Read stderr log file for a preview and emit terminal events. Best-effort."""
    if queue is None:
        return
    import glob
    await asyncio.sleep(2.0)  # Give process time to write something
    for pattern in ["/tmp/dev_agent_preview_py_*", "/tmp/dev_agent_preview_node_*",
                    "/tmp/dev_agent_preview_static_*"]:
        for d in glob.glob(pattern):
            log_path = os.path.join(d, "_preview_stderr.log")
            if os.path.isfile(log_path):
                try:
                    with open(log_path, encoding="utf-8") as f:
                        content = f.read(4000)
                    if content.strip():
                        try:
                            queue.put_nowait({
                                "event": "terminal",
                                "data": {"source": "preview", "text": content.strip()},
                            })
                        except asyncio.QueueFull:
                            pass
                except OSError:
                    pass


def _read_preview_stderr(pid: int) -> str:
    """Attempt to read the stderr log from a preview tmpdir."""
    import glob
    for pattern in ["/tmp/dev_agent_preview_py_*", "/tmp/dev_agent_preview_node_*",
                    "/tmp/dev_agent_preview_static_*"]:
        for d in glob.glob(pattern):
            log_path = os.path.join(d, "_preview_stderr.log")
            if os.path.isfile(log_path):
                try:
                    with open(log_path, encoding="utf-8") as f:
                        content = f.read(2000)
                    if content.strip():
                        return content.strip()
                except OSError:
                    pass
    return ""


def stop_preview(session_id: str, runner: SandboxRunner) -> None:
    """Stop a preview server and free its resources."""
    pid = _active_pids.pop(session_id, None)
    if pid:
        try:
            runner.stop_preview(pid)
        except Exception as exc:
            logger.warning("Failed to stop preview PID %d: %s", pid, exc)
    release_port(session_id)
    # Clean up tmpdir if tracked
    tmpdir = _preview_dirs.pop(session_id, None)
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)


def cleanup_all() -> None:
    """Kill all preview servers — called on app shutdown."""
    for _session_id, pid in list(_active_pids.items()):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    _active_pids.clear()
    _allocated_ports.clear()
    for tmpdir in _preview_dirs.values():
        shutil.rmtree(tmpdir, ignore_errors=True)
    _preview_dirs.clear()
    logger.info("All preview servers cleaned up")


def get_active_sessions() -> list[str]:
    """Return session IDs with active previews."""
    return list(_active_pids.keys())
