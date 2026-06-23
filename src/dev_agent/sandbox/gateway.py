"""Execution gateway — routes generated-code execution local or to the sandbox.

When ``SANDBOX_URL`` is set (docker-compose), all test runs and previews are
delegated to the isolated ``sandbox`` service, which holds no secrets. When it
is unset (local dev / unit tests), execution runs in-process exactly as before.
This keeps a single code path for callers while making isolation a deployment
concern.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from functools import partial
from typing import Any

import httpx
from fastapi import HTTPException
from fastapi.responses import Response

from src.dev_agent.pipeline.executor import get_runner, get_runner_for_files
from src.dev_agent.sandbox import preview_server
from src.dev_agent.sandbox.base import TestReport
from src.dev_agent.sandbox.console_inject import inject_console_capture

logger = logging.getLogger(__name__)

_SANDBOX_URL = os.environ.get("SANDBOX_URL", "").rstrip("/")
# Sessions with a preview started via the remote sandbox (local mode tracks
# state inside preview_server instead).
_remote_active: set[str] = set()


def is_remote() -> bool:
    return bool(_SANDBOX_URL)


# ── Tests ───────────────────────────────────────────────────────────────────────

async def run_tests(
    files: dict[str, str],
    runtime: str,
    event_queue: asyncio.Queue[dict[str, Any]] | None,
) -> TestReport:
    """Run tests, streaming terminal events to ``event_queue``. Returns a TestReport."""
    if not is_remote():
        runner = get_runner(runtime)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(runner.run_tests, files, event_queue))

    # Remote: stream NDJSON; re-emit terminal lines, capture the final report.
    report_data: dict[str, Any] | None = None
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST", f"{_SANDBOX_URL}/run-tests",
            json={"files": files, "runtime": runtime},
        ) as resp:
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("event") == "report":
                    report_data = event["report"]
                elif event_queue is not None:
                    try:
                        event_queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass
    if report_data is None:
        return TestReport(
            has_critical_bugs=True, passed_count=0, failed_count=0, error_count=1,
            output_summary="Sandbox service returned no test report", test_cases=[],
            execution_time_ms=0,
        )
    return TestReport(**report_data)


# ── Preview ─────────────────────────────────────────────────────────────────────

async def preview_start(session_id: str, files: dict[str, str], runtime: str) -> dict[str, Any]:
    """Start a preview. Raises RuntimeError(detail) on failure."""
    if not is_remote():
        runner = get_runner_for_files(runtime, files)
        return await preview_server.start_preview(session_id, files, runner, runtime)  # type: ignore[return-value]

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{_SANDBOX_URL}/preview/start",
            json={"session_id": session_id, "files": files, "runtime": runtime},
        )
    if resp.status_code == 503:
        raise RuntimeError(_detail(resp))
    resp.raise_for_status()
    _remote_active.add(session_id)
    return resp.json()


async def preview_stop(session_id: str, files: dict[str, str], runtime: str) -> None:
    if not is_remote():
        runner = get_runner_for_files(runtime, files)
        preview_server.stop_preview(session_id, runner)
        return
    _remote_active.discard(session_id)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.post(
                f"{_SANDBOX_URL}/preview/stop",
                json={"session_id": session_id, "files": files, "runtime": runtime},
            )
    except httpx.HTTPError as exc:
        logger.warning("Remote preview stop failed for %s: %s", session_id[:12], exc)


def is_active(session_id: str) -> bool:
    if not is_remote():
        return preview_server.get_preview_mode(session_id) is not None
    return session_id in _remote_active


async def serve(session_id: str, path: str, method: str, body: bytes, query: str) -> Response:
    """Serve a preview request, injecting console-capture into HTML responses."""
    if not is_remote():
        return await _serve_local(session_id, path, method, body, query)

    if session_id not in _remote_active:
        raise HTTPException(404, "No preview running for this session")
    url = f"{_SANDBOX_URL}/preview/{session_id}/{path}"
    if query:
        url += f"?{query}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.request(method, url, content=body)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Sandbox preview unreachable: {exc}") from None
    content = resp.content
    if "text/html" in resp.headers.get("content-type", ""):
        content = inject_console_capture(content)
    return Response(content=content, status_code=resp.status_code, headers=_proxy_headers(resp))


async def _serve_local(session_id: str, path: str, method: str, body: bytes, query: str) -> Response:
    import mimetypes

    mode = preview_server.get_preview_mode(session_id)
    if mode is None:
        raise HTTPException(404, "No preview running for this session")

    if mode == "static":
        target = preview_server.resolve_static_file(session_id, path)
        if target is None:
            raise HTTPException(404, "File not found in preview")
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/html"):
            data = inject_console_capture(data)
        return Response(content=data, media_type=ctype)

    port = preview_server._allocated_ports[session_id]
    target_url = f"http://127.0.0.1:{port}/{path}"
    if query:
        target_url += f"?{query}"
    backoff = [0.3, 0.6, 1.2]
    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt, delay in enumerate(backoff):
            try:
                resp = await client.request(method, target_url, content=body)
                content = resp.content
                if "text/html" in resp.headers.get("content-type", ""):
                    content = inject_console_capture(content)
                return Response(content=content, status_code=resp.status_code, headers=_proxy_headers(resp))
            except httpx.ConnectError:
                if attempt < len(backoff) - 1:
                    await asyncio.sleep(delay)
    raise HTTPException(502, f"Preview server on port {port} not responding") from None


# Headers that must not be forwarded verbatim: httpx has already decoded the
# body, so the upstream length/encoding/transfer headers would be wrong.
_HOP_BY_HOP = {"content-length", "content-encoding", "transfer-encoding", "connection"}


def _proxy_headers(resp: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", resp.text))
    except (json.JSONDecodeError, ValueError):
        return resp.text
