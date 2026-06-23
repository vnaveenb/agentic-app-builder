"""Preview lifecycle manager — tiered serving, port allocation, PID tracking, cleanup.

Two preview modes:

* ``static`` — pure front-end file sets (HTML/CSS/JS, React/Angular via CDN).
  Files are written to a tracked dir and served *directly* by the web layer.
  No subprocess, no port binding — so the whole class of "server bound the
  wrong port" failures is impossible for the common case.
* ``server`` — Flask/FastAPI/Node apps that genuinely need a running process.
  A subprocess is spawned with the port enforced by the runner, then we wait
  for it to bind AND serve a request before reporting success.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import shutil
import signal
import socket
import tempfile
import time

import httpx

from src.dev_agent.sandbox.base import SandboxRunner

logger = logging.getLogger(__name__)

_PORT_RANGE = range(9100, 9121)  # 20 concurrent previews max
_allocated_ports: dict[str, int] = {}  # session_id → port (server mode only)
_active_pids: dict[str, int] = {}  # session_id → PID (server mode only)
_preview_dirs: dict[str, str] = {}  # session_id → tmpdir path
_preview_mode: dict[str, str] = {}  # session_id → "static" | "server"


# ── Tiering ────────────────────────────────────────────────────────────────────

def is_static_preview(runtime: str, files: dict[str, str]) -> bool:
    """True if the file set is pure front-end and can be served without a process.

    Decided by *content*, not just filenames — ``app.js``/``index.js`` are common
    client-side script names and must not be mistaken for Node servers. A genuine
    server entry point (Flask/FastAPI/Starlette/uvicorn in a ``.py``; an actual
    ``http`` listener in a ``.js``) forces ``server`` mode. Otherwise, anything
    with an ``index.html`` is served statically.
    """
    has_index = any(f.endswith("index.html") for f in files)

    # React/Angular/static are CDN/front-end by contract — never spawn a process.
    if runtime in ("static", "react", "angular"):
        return has_index

    py_server = any(
        f.endswith(".py")
        and any(tok in content for tok in ("Flask", "FastAPI", "Starlette", "uvicorn"))
        for f, content in files.items()
    )
    if py_server:
        return False

    node_server = any(
        f.endswith(".js")
        and any(tok in content for tok in (".listen(", "createServer", "express(", "http.Server"))
        for f, content in files.items()
    )
    if node_server:
        return False

    return has_index


def get_preview_mode(session_id: str) -> str | None:
    return _preview_mode.get(session_id)


# ── Port allocation (server mode) ───────────────────────────────────────────────

def allocate_port(session_id: str) -> int:
    """Allocate a unique preview port for a session. Raises RuntimeError if exhausted."""
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


async def _wait_for_port(port: int, timeout: float = 20.0) -> bool:
    """Poll until a port is accepting TCP connections (or timeout)."""
    deadline = time.monotonic() + timeout
    sleep_interval = 0.15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(sleep_interval)
            sleep_interval = min(sleep_interval * 1.5, 1.0)
    return False


async def _smoke_check(port: int, timeout: float = 5.0) -> tuple[bool, str]:
    """Issue one GET / to confirm the server actually serves (not just binds).

    Returns (ok, detail). ``ok`` is True for any HTTP response < 500 — a 404 is
    fine (the app is up, just no root route); a 5xx or connection error is not.
    """
    url = f"http://127.0.0.1:{port}/"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        if resp.status_code >= 500:
            return False, f"server returned {resp.status_code} on GET /"
        return True, ""
    except httpx.HTTPError as exc:
        return False, f"GET / failed: {exc!s}"


# ── Static mode ─────────────────────────────────────────────────────────────────

def _start_static(session_id: str, files: dict[str, str]) -> dict[str, object]:
    """Write files to a tracked dir for direct serving. No subprocess."""
    tmpdir = tempfile.mkdtemp(prefix="dev_agent_preview_static_")
    tmp = pathlib.Path(tmpdir)
    for name, content in files.items():
        path = tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # Ensure a root index.html exists even if it was generated nested.
    if not (tmp / "index.html").exists():
        for name in files:
            if name.endswith("index.html"):
                (tmp / "index.html").write_text(files[name], encoding="utf-8")
                break

    _preview_dirs[session_id] = tmpdir
    _preview_mode[session_id] = "static"
    logger.info("Static preview ready for session=%s (%d files, no subprocess)",
                session_id[:12], len(files))
    return {"url": f"/preview/{session_id}", "status": "running", "mode": "static"}


def resolve_static_file(session_id: str, path: str) -> pathlib.Path | None:
    """Resolve a request path to a file inside the session's static dir (safe-join).

    Empty/`/` paths resolve to index.html. Returns None on traversal attempts or
    missing files.
    """
    tmpdir = _preview_dirs.get(session_id)
    if not tmpdir:
        return None
    root = pathlib.Path(tmpdir).resolve()
    rel = path.strip("/") or "index.html"
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        return None  # path traversal
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate if candidate.is_file() else None


# ── Server mode ─────────────────────────────────────────────────────────────────

async def _start_server(
    session_id: str,
    files: dict[str, str],
    runner: SandboxRunner,
) -> dict[str, object]:
    """Spawn a subprocess server, wait for it to bind AND serve, then report."""
    port = allocate_port(session_id)
    logger.info("Starting server preview for session=%s on port=%d (%d files, runner=%s)",
                session_id[:12], port, len(files), type(runner).__name__)

    loop = asyncio.get_event_loop()
    preview_info = await loop.run_in_executor(None, runner.start_preview, files, port)

    pid = preview_info.pid
    tmpdir = preview_info.tmpdir
    _active_pids[session_id] = pid
    _preview_dirs[session_id] = tmpdir
    _preview_mode[session_id] = "server"

    def _fail(detail: str) -> None:
        _active_pids.pop(session_id, None)
        _preview_dirs.pop(session_id, None)
        _preview_mode.pop(session_id, None)
        release_port(session_id)
        stderr_msg = _read_preview_stderr(session_id)
        full = detail if not stderr_msg else f"{detail}: {stderr_msg}"
        raise RuntimeError(full) from None

    # 1) Wait for the TCP port to bind.
    if not await _wait_for_port(port, timeout=20.0):
        try:
            os.kill(pid, 0)
            _fail(f"Preview server never bound port {port} within 20s")
        except OSError:
            _fail(f"Preview process exited before binding port {port}")

    # 2) Confirm it actually serves a request (catches crash-on-first-request).
    ok, detail = await _smoke_check(port)
    if not ok:
        try:
            os.kill(pid, 0)
            _fail(f"Preview server bound port {port} but {detail}")
        except OSError:
            _fail(f"Preview process exited after binding port {port}")

    logger.info("Server preview ready: session=%s pid=%d port=%d", session_id[:12], pid, port)
    return {"port": port, "url": f"/preview/{session_id}", "status": "running",
            "pid": pid, "mode": "server"}


# ── Public API ──────────────────────────────────────────────────────────────────

async def start_preview(
    session_id: str,
    files: dict[str, str],
    runner: SandboxRunner,
    runtime: str = "",
) -> dict[str, object]:
    """Start a preview, choosing static-serve or a server subprocess by file set."""
    if is_static_preview(runtime, files):
        return _start_static(session_id, files)
    return await _start_server(session_id, files, runner)


async def stream_preview_stderr(
    session_id: str,
    queue: asyncio.Queue | None,
) -> None:
    """Read the stderr log for a preview and emit terminal events. Best-effort."""
    if queue is None:
        return
    await asyncio.sleep(2.0)
    content = _read_preview_stderr(session_id, limit=4000)
    if content:
        try:
            queue.put_nowait({
                "event": "terminal",
                "data": {"source": "preview", "text": content},
            })
        except asyncio.QueueFull:
            pass


def _read_preview_stderr(session_id: str, limit: int = 2000) -> str:
    """Read the stderr log from the session's tracked preview dir."""
    tmpdir = _preview_dirs.get(session_id)
    if not tmpdir:
        return ""
    log_path = os.path.join(tmpdir, "_preview_stderr.log")
    if not os.path.isfile(log_path):
        return ""
    try:
        with open(log_path, encoding="utf-8") as f:
            content = f.read(limit)
        return content.strip()
    except OSError:
        return ""


def stop_preview(session_id: str, runner: SandboxRunner) -> None:
    """Stop a preview server (if any) and free its resources."""
    pid = _active_pids.pop(session_id, None)
    if pid:
        try:
            runner.stop_preview(pid)
        except Exception as exc:
            logger.warning("Failed to stop preview PID %d: %s", pid, exc)
    release_port(session_id)
    _preview_mode.pop(session_id, None)
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
    _preview_mode.clear()
    logger.info("All preview servers cleaned up")


def get_active_sessions() -> list[str]:
    """Return session IDs with active previews (static or server)."""
    return list(_preview_mode.keys())
