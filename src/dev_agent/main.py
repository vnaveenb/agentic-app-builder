"""FastAPI application entry point — Project 7: AI Dev Agent."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import pathlib
import uuid
import zipfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from src.dev_agent.pipeline.base import PipelineBackend
from src.dev_agent.pipeline.crew_backend import CrewAIBackend
from src.dev_agent.pipeline.executor import get_runner, get_runner_for_files
from src.dev_agent.pipeline.graph import LangGraphBackend
from src.dev_agent.pipeline.state import DevPipelineState
from src.dev_agent.sandbox import preview_server
from src.dev_agent.schemas import (
    GenerateRequest,
    GenerateResponse,
    HealthResponse,
    IterateRequest,
    PreviewStartResponse,
    StatusResponse,
    UpdateFilesRequest,
)

logger = logging.getLogger(__name__)

_UI = pathlib.Path(__file__).parent / "static" / "index.html"
_SUPPORTED_RUNTIMES = ["python", "node", "react", "angular", "static"]

# ── Backend factory ───────────────────────────────────────────────────────────

_backends: dict[str, PipelineBackend] = {
    "langgraph": LangGraphBackend(),
    "crewai": CrewAIBackend(),
}


def get_backend(name: str) -> PipelineBackend:
    """Return the requested pipeline backend, defaulting to LangGraph."""
    return _backends.get(name, _backends["langgraph"])


# ── In-memory session stores ──────────────────────────────────────────────────

_event_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
_session_states: dict[str, DevPipelineState] = {}
_session_histories: dict[str, list[dict[str, Any]]] = {}
_session_backends: dict[str, str] = {}


# ── Console capture injection ─────────────────────────────────────────────────

_CONSOLE_CAPTURE_SCRIPT = b"""<script>
(function(){
  var _origError = console.error;
  var _origWarn = console.warn;
  function _send(level, args) {
    try {
      var msg = Array.prototype.slice.call(args).map(function(a) {
        return typeof a === 'object' ? JSON.stringify(a) : String(a);
      }).join(' ');
      window.parent.postMessage({type:'console', level:level, msg:msg}, '*');
    } catch(e) {}
  }
  console.error = function() { _send('error', arguments); _origError.apply(console, arguments); };
  console.warn = function() { _send('warn', arguments); _origWarn.apply(console, arguments); };
  window.onerror = function(msg, src, line, col, err) {
    _send('error', [msg + ' (' + (src||'') + ':' + line + ':' + col + ')']);
  };
  window.onunhandledrejection = function(e) {
    _send('error', ['Unhandled Promise: ' + (e.reason || e)]);
  };
})();
</script>
"""


def _inject_console_capture(content: bytes) -> bytes:
    """Inject console-capture script into HTML after <head> or <body>."""
    lower = content.lower()
    idx = lower.find(b"<head>")
    if idx != -1:
        insert_at = idx + len(b"<head>")
        return content[:insert_at] + _CONSOLE_CAPTURE_SCRIPT + content[insert_at:]
    idx = lower.find(b"<body")
    if idx != -1:
        close = lower.find(b">", idx)
        if close != -1:
            insert_at = close + 1
            return content[:insert_at] + _CONSOLE_CAPTURE_SCRIPT + content[insert_at:]
    return _CONSOLE_CAPTURE_SCRIPT + content


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Project 7 — AI Dev Agent starting up")
    yield
    # Cleanup all preview servers on shutdown
    preview_server.cleanup_all()
    logger.info("Project 7 — AI Dev Agent shut down, previews cleaned")


app = FastAPI(title="project-7-ai-dev-agent", lifespan=lifespan)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    provider = os.environ.get("LLM_PROVIDER", "google_genai:gemini-2.0-flash")
    return HealthResponse(
        status="ok",
        project="project-7-ai-dev-agent",
        provider=provider,
        runtimes=_SUPPORTED_RUNTIMES,
    )


@app.get("/")
def ui() -> FileResponse:
    return FileResponse(str(_UI))


@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest) -> GenerateResponse:
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _event_queues[session_id] = queue

    initial_state: DevPipelineState = {
        "session_id": session_id,
        "idea": req.idea,
        "runtime": req.runtime,
        "plan": None,
        "files": {},
        "test_report": None,
        "preview": None,
        "review_notes": [],
        "iteration": 0,
        "max_iterations": req.max_iterations,
        "status": "running",
        "errors": [],
        "current_agent": "",
        "event_queue": queue,
        "user_feedback": "",
    }
    _session_states[session_id] = initial_state
    _session_backends[session_id] = req.backend

    asyncio.create_task(_run_pipeline_task(session_id, initial_state, queue, req.backend))

    return GenerateResponse(session_id=session_id)


@app.get("/stream/{session_id}")
async def stream(session_id: str) -> StreamingResponse:
    queue = _event_queues.get(session_id)
    if not queue:
        raise HTTPException(404, "Session not found")

    async def event_generator() -> AsyncGenerator[str, None]:
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("event") in ("pipeline_done", "error"):
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/status/{session_id}", response_model=StatusResponse)
def status(session_id: str) -> StatusResponse:
    state = _session_states.get(session_id)
    if not state:
        raise HTTPException(404, "Session not found")
    return StatusResponse(
        session_id=session_id,
        status=state["status"],
        current_agent=state["current_agent"],
        iteration=state["iteration"],
        max_iterations=state["max_iterations"],
        runtime=state["runtime"],
        errors=state["errors"],
    )


@app.get("/download/{session_id}")
def download(session_id: str) -> StreamingResponse:
    state = _session_states.get(session_id)
    if not state:
        raise HTTPException(404, "Session not found")
    if state["status"] != "done":
        raise HTTPException(400, "Pipeline not complete yet")

    files = state["files"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    buf.seek(0)

    app_name = "project"
    plan = state.get("plan")
    if plan:
        app_name = plan.app_name

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{app_name}.zip"'},
    )


# ── Iterate endpoint ──────────────────────────────────────────────────────────


@app.put("/files/{session_id}")
async def update_files(session_id: str, req: UpdateFilesRequest) -> dict[str, str]:
    """Update session files (from editor) and hot-reload preview if running."""
    state = _session_states.get(session_id)
    if not state:
        raise HTTPException(404, "Session not found")

    state["files"] = req.files

    # If preview is active, restart it with new files
    if session_id in preview_server._allocated_ports:
        runner = get_runner(state["runtime"])
        preview_server.stop_preview(session_id, runner)
        try:
            await preview_server.start_preview(session_id, req.files, runner)
        except RuntimeError:
            pass  # Preview restart failed — user can retry manually

        # Notify via event queue
        queue = _event_queues.get(session_id)
        if queue:
            try:
                queue.put_nowait({"event": "preview_reload"})
            except asyncio.QueueFull:
                pass

    return {"status": "updated"}


@app.post("/iterate/{session_id}", response_model=GenerateResponse)
async def iterate(session_id: str, req: IterateRequest) -> GenerateResponse:
    """Re-run developer → tester → reviewer with user feedback (skips planner)."""
    state = _session_states.get(session_id)
    if not state:
        raise HTTPException(404, "Session not found")
    if state["status"] not in ("done", "error"):
        raise HTTPException(400, "Pipeline still running — wait for completion")
    if not state.get("plan"):
        raise HTTPException(400, "No plan found — run /generate first")

    # Stop any active preview
    runner = get_runner(state["runtime"])
    preview_server.stop_preview(session_id, runner)

    # Set up new event queue for streaming
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _event_queues[session_id] = queue

    # Update state for iteration
    state["status"] = "running"
    state["user_feedback"] = req.feedback
    state["iteration"] = 0
    state["test_report"] = None
    state["errors"] = []
    state["event_queue"] = queue

    asyncio.create_task(_run_iterate_task(session_id, state, queue, _session_backends.get(session_id, "langgraph")))

    return GenerateResponse(session_id=session_id)


@app.get("/history/{session_id}")
def get_history(session_id: str) -> dict[str, Any]:
    history = _session_histories.get(session_id, [])
    # Return minimal metadata to populate a UI list
    versions = [
        {"version": h["version"], "timestamp": h.get("timestamp", ""), "description": h.get("description", f"Version {h['version']}"), "is_current": h.get("is_current", False)}
        for h in history
    ]
    return {"versions": versions}


@app.post("/checkout/{session_id}/{version}")
async def checkout_version(session_id: str, version: int) -> dict[str, Any]:
    state = _session_states.get(session_id)
    history = _session_histories.get(session_id, [])
    if not state or not history:
        raise HTTPException(404, "Session or history not found")

    target = next((h for h in history if h["version"] == version), None)
    if not target:
        raise HTTPException(404, "Version not found")

    state["files"] = target["files"]
    for h in history:
        h["is_current"] = (h["version"] == version)

    # If preview is active, restart it with new files
    if session_id in preview_server._allocated_ports:
        runner = get_runner(state["runtime"])
        preview_server.stop_preview(session_id, runner)
        try:
            await preview_server.start_preview(session_id, state["files"], runner)
        except RuntimeError:
            pass

        queue = _event_queues.get(session_id)
        if queue:
            try:
                queue.put_nowait({"event": "preview_reload"})
            except asyncio.QueueFull:
                pass

    return {"status": "checked_out", "version": version, "files": state["files"]}


# ── Preview endpoints ─────────────────────────────────────────────────────────


@app.post("/preview/{session_id}/start", response_model=PreviewStartResponse)
async def preview_start(session_id: str) -> PreviewStartResponse:
    state = _session_states.get(session_id)
    if not state:
        raise HTTPException(404, "Session not found")
    if state["status"] != "done":
        raise HTTPException(400, "Pipeline not complete — cannot start preview")

    # Use smart runner selection that considers both runtime AND actual files
    runner = get_runner_for_files(state["runtime"], state["files"])
    logger.info("Preview start: runtime=%s, runner=%s, files=%s",
                state["runtime"], type(runner).__name__, list(state["files"].keys()))
    try:
        info = await preview_server.start_preview(session_id, state["files"], runner)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None

    return PreviewStartResponse(
        port=info["port"],  # type: ignore[arg-type]
        url=info["url"],  # type: ignore[arg-type]
        status=info["status"],  # type: ignore[arg-type]
    )


@app.post("/preview/{session_id}/stop")
def preview_stop(session_id: str) -> dict[str, str]:
    state = _session_states.get(session_id)
    if not state:
        raise HTTPException(404, "Session not found")

    runner = get_runner(state["runtime"])
    preview_server.stop_preview(session_id, runner)
    return {"status": "stopped"}


@app.api_route(
    "/preview/{session_id}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
)
async def preview_proxy(session_id: str, path: str, request: Request) -> Response:
    """Reverse-proxy requests to the session's ephemeral preview server."""
    if session_id not in preview_server._allocated_ports:
        raise HTTPException(404, "No preview running for this session")

    port = preview_server._allocated_ports[session_id]
    target_url = f"http://127.0.0.1:{port}/{path}"

    # Retry with exponential backoff — the preview may still be binding
    body = await request.body()
    backoff_schedule = [0.5, 1.0, 2.0, 3.0, 4.0]  # 5 retries, ~10.5s total
    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt, delay in enumerate(backoff_schedule):
            try:
                resp = await client.request(
                    method=request.method,
                    url=target_url,
                    content=body,
                )
                content = resp.content
                headers = dict(resp.headers)

                # Inject console-capture script into HTML responses
                ct = headers.get("content-type", "")
                if "text/html" in ct:
                    content = _inject_console_capture(content)
                    headers["content-length"] = str(len(content))

                return Response(
                    content=content,
                    status_code=resp.status_code,
                    headers=headers,
                )
            except httpx.ConnectError:
                if attempt < len(backoff_schedule) - 1:
                    await asyncio.sleep(delay)

    # All retries failed — check if the process died
    pid = preview_server._active_pids.get(session_id)
    if pid:
        try:
            os.kill(pid, 0)
        except OSError:
            logger.error("Preview process pid=%d crashed for session %s", pid, session_id[:12])
            raise HTTPException(
                502, "Preview process crashed — check logs for details"
            ) from None
    logger.error("Preview proxy exhausted retries for session %s port %d", session_id[:12], port)
    raise HTTPException(502, f"Preview server on port {port} not responding after {len(backoff_schedule)} retries") from None


# ── Background task ───────────────────────────────────────────────────────────


async def _run_pipeline_task(
    session_id: str,
    initial_state: DevPipelineState,
    queue: asyncio.Queue[dict[str, Any]],
    backend_name: str = "langgraph",
) -> None:
    """Run the full agent pipeline as a background task."""
    try:
        backend = get_backend(backend_name)
        final_state = await backend.run(initial_state, queue)

        _session_states[session_id] = final_state
        _session_states[session_id]["status"] = "done"

        # Record initial version in history
        if session_id not in _session_histories:
            _session_histories[session_id] = []
        _session_histories[session_id].append({
            "version": 1,
            "description": f"Initial Build ({backend_name})",
            "files": dict(final_state.get("files", {})),
            "is_current": True
        })

        # Push completion event with files embedded
        await queue.put({
            "event": "pipeline_done",
            "files": final_state["files"],
            "runtime": final_state["runtime"],
            "review_notes": final_state.get("review_notes", []),
            "backend": backend_name,
        })

    except Exception as exc:
        logger.exception("Pipeline failed for session %s", session_id)
        _session_states[session_id]["status"] = "error"
        _session_states[session_id]["errors"].append(str(exc))
        await queue.put({
            "event": "error",
            "message": str(exc),
        })


async def _run_iterate_task(
    session_id: str,
    state: DevPipelineState,
    queue: asyncio.Queue[dict[str, Any]],
    backend_name: str = "langgraph",
) -> None:
    """Run the iterate pipeline (developer → tester → reviewer) as a background task."""
    try:
        backend = get_backend(backend_name)
        final_state = await backend.run_iterate(state, queue)

        _session_states[session_id] = final_state
        _session_states[session_id]["status"] = "done"
        _session_states[session_id]["user_feedback"] = ""  # Clear after use

        # Record iteration in history
        history = _session_histories.get(session_id, [])
        v_num = len(history) + 1
        for h in history:
            h["is_current"] = False

        history.append({
            "version": v_num,
            "description": f"Iteration {v_num - 1} ({backend_name})",
            "files": dict(final_state.get("files", {})),
            "is_current": True
        })
        _session_histories[session_id] = history

        await queue.put({
            "event": "pipeline_done",
            "files": final_state["files"],
            "runtime": final_state["runtime"],
            "review_notes": final_state.get("review_notes", []),
            "backend": backend_name,
        })

    except Exception as exc:
        logger.exception("Iterate pipeline failed for session %s", session_id)
        _session_states[session_id]["status"] = "error"
        _session_states[session_id]["errors"].append(str(exc))
        await queue.put({
            "event": "error",
            "message": str(exc),
        })
