"""Sandbox service — FastAPI worker that owns all generated-code execution.

Endpoints:
* ``GET  /health``                       — liveness.
* ``POST /run-tests``                     — run the test suite for a file set;
  streams terminal output as NDJSON, ending with a ``report`` line.
* ``POST /preview/start``                 — start a tiered preview (static-serve
  or subprocess); returns mode/url/port or a 503 with the real error.
* ``POST /preview/stop``                  — tear a preview down.
* ``ANY  /preview/{session_id}/{path}``   — serve the preview (static files
  directly, or reverse-proxy to the spawned subprocess).

This module deliberately imports NOTHING from ``src.dev_agent`` that touches
secrets, the database, or Redis — only the pure sandbox runners.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict
from functools import partial
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from src.dev_agent.pipeline.executor import get_runner, get_runner_for_files
from src.dev_agent.sandbox import preview_server

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sandbox_service")

app = FastAPI(title="AI Dev Agent — Sandbox Service")


class RunTestsRequest(BaseModel):
    files: dict[str, str]
    runtime: str = "auto"


class PreviewStartRequest(BaseModel):
    session_id: str
    files: dict[str, str]
    runtime: str = "auto"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "active_previews": str(len(preview_server.get_active_sessions()))}


@app.post("/run-tests")
async def run_tests(req: RunTestsRequest) -> StreamingResponse:
    """Run tests in a thread, streaming terminal events then the final report (NDJSON)."""
    runner = get_runner(req.runtime)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def gen() -> Any:
        loop = asyncio.get_event_loop()
        task = loop.run_in_executor(None, partial(runner.run_tests, req.files, queue))
        # Drain terminal events while the runner works.
        while True:
            try:
                event = queue.get_nowait()
                yield json.dumps(event) + "\n"
            except asyncio.QueueEmpty:
                if task.done():
                    break
                await asyncio.sleep(0.05)
        report = await task
        yield json.dumps({"event": "report", "report": asdict(report)}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/preview/start")
async def preview_start(req: PreviewStartRequest) -> dict[str, Any]:
    runner = get_runner_for_files(req.runtime, req.files)
    logger.info("preview/start session=%s runtime=%s runner=%s",
                req.session_id[:12], req.runtime, type(runner).__name__)
    try:
        return await preview_server.start_preview(
            req.session_id, req.files, runner, req.runtime
        )  # type: ignore[return-value]
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None


@app.post("/preview/stop")
def preview_stop(req: PreviewStartRequest) -> dict[str, str]:
    runner = get_runner_for_files(req.runtime, req.files)
    preview_server.stop_preview(req.session_id, runner)
    return {"status": "stopped"}


@app.api_route("/preview/{session_id}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def preview_serve(session_id: str, path: str, request: Request) -> Response:
    """Serve the preview — static files directly, or proxy to the subprocess.

    Returns raw bytes; the app edge is responsible for console-capture injection.
    """
    mode = preview_server.get_preview_mode(session_id)
    if mode is None:
        raise HTTPException(404, "No preview running for this session")

    if mode == "static":
        target = preview_server.resolve_static_file(session_id, path)
        if target is None:
            raise HTTPException(404, "File not found in preview")
        import mimetypes
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return Response(content=target.read_bytes(), media_type=ctype)

    port = preview_server._allocated_ports[session_id]
    target_url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"
    body = await request.body()
    backoff = [0.3, 0.6, 1.2]
    hop_by_hop = {"content-length", "content-encoding", "transfer-encoding", "connection"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt, delay in enumerate(backoff):
            try:
                resp = await client.request(request.method, target_url, content=body)
                headers = {k: v for k, v in resp.headers.items() if k.lower() not in hop_by_hop}
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    headers=headers,
                )
            except httpx.ConnectError:
                if attempt < len(backoff) - 1:
                    await asyncio.sleep(delay)
    raise HTTPException(502, f"Preview server on port {port} not responding") from None


@app.on_event("shutdown")
def _shutdown() -> None:
    preview_server.cleanup_all()


# Bind range for in-container preview subprocesses (not published to the host).
os.environ.setdefault("SANDBOX_PREVIEW_PORTS", "9100-9120")
