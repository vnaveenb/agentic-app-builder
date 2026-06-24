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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text as select_text

from src.dev_agent import config
from src.dev_agent.cache import (
    cache_session_state,
    close_redis,
)
from src.dev_agent.cache import (
    health_check as redis_health_check,
)
from src.dev_agent.db.database import async_session_factory, close_db, init_db
from src.dev_agent.db.models import Session as DBSession
from src.dev_agent.llm import LLMContext, build_llm_context
from src.dev_agent.agents.planner import planner_node
from src.dev_agent.pipeline.base import PipelineBackend
from src.dev_agent.pipeline.crew_backend import CrewAIBackend
from src.dev_agent.pipeline.graph import LangGraphBackend
from src.dev_agent.pipeline.state import DevPipelineState, Plan
from src.dev_agent.sandbox import gateway, preview_server
from src.dev_agent.schemas import (
    ChatHistoryResponse,
    ChatMessageRequest,
    ChatMessageSchema,
    ChatResponse,
    DiffResponse,
    FileDiffSchema,
    GenerateRequest,
    GenerateResponse,
    HealthResponse,
    IterateRequest,
    MemoryCreateRequest,
    MemoryListResponse,
    MemorySchema,
    PreviewStartResponse,
    ProviderInfo,
    ProviderKeyRequest,
    ProvidersResponse,
    SessionListResponse,
    SessionSummary,
    StatusResponse,
    UpdateFilesRequest,
)
from src.dev_agent.versioning.differ import compute_diff
from src.dev_agent.versioning.store import (
    checkout_version as db_checkout_version,
)
from src.dev_agent.versioning.store import (
    create_version,
    get_next_version_number,
    get_version,
    get_versions,
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


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Project 7 — AI Dev Agent starting up")
    await init_db()
    logger.info("Database initialized")
    yield
    # Cleanup all preview servers on shutdown
    preview_server.cleanup_all()
    await close_db()
    await close_redis()
    logger.info("Project 7 — AI Dev Agent shut down, previews cleaned")


app = FastAPI(title="project-7-ai-dev-agent", lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=str(pathlib.Path(__file__).parent / "static")),
    name="static",
)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    try:
        defaults = config.get_default()
        provider = f"{defaults.provider}:{defaults.model}"
    except Exception:
        provider = os.environ.get("LLM_PROVIDER", "google_genai:gemini-3.5-flash")
    redis_ok = await redis_health_check()
    db_ok = True
    try:
        async with async_session_factory() as session:
            await session.execute(select_text("SELECT 1"))
    except Exception:
        db_ok = False
    return HealthResponse(
        status="ok" if (redis_ok and db_ok) else "degraded",
        project="project-7-ai-dev-agent",
        provider=provider,
        runtimes=_SUPPORTED_RUNTIMES,
        database="connected" if db_ok else "disconnected",
        redis="connected" if redis_ok else "disconnected",
    )


@app.get("/")
def ui() -> FileResponse:
    return FileResponse(str(_UI))


@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest) -> GenerateResponse:
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _event_queues[session_id] = queue

    llm_context = await build_llm_context(
        req.client_id, req.provider, req.model, req.api_key
    )

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
        "llm_context": llm_context,
    }
    _session_states[session_id] = initial_state
    _session_backends[session_id] = req.backend

    # Create session record in DB immediately (so chat/messages can reference it)
    try:
        async with async_session_factory() as db:
            db_session = DBSession(
                id=uuid.UUID(session_id),
                idea=req.idea,
                runtime=req.runtime,
                backend=req.backend,
                status="running",
                max_iterations=req.max_iterations,
            )
            db.add(db_session)
            await db.commit()
    except Exception as db_exc:
        logger.warning("Failed to create session in DB: %s", db_exc)

    asyncio.create_task(_run_pipeline_task(session_id, initial_state, queue, req.backend))

    return GenerateResponse(session_id=session_id)


@app.post("/approve-plan/{session_id}")
async def approve_plan(session_id: str) -> dict[str, str]:
    """Approve the plan and start the build pipeline."""
    state = _session_states.get(session_id)
    if not state:
        raise HTTPException(404, "Session not found")
    if state.get("status") != "awaiting_approval":
        raise HTTPException(409, f"Session is not awaiting approval (status={state.get('status')})")

    queue = _event_queues.get(session_id)
    if not queue:
        queue = asyncio.Queue()
        _event_queues[session_id] = queue

    state["status"] = "running"
    state["iteration"] = 0
    backend_name = _session_backends.get(session_id, "langgraph")

    asyncio.create_task(_run_build_task(session_id, state, queue, backend_name))

    return {"status": "approved", "session_id": session_id}


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
    ctx = state.get("llm_context")
    defaults = config.get_default()
    return StatusResponse(
        session_id=session_id,
        status=state["status"],
        current_agent=state["current_agent"],
        iteration=state["iteration"],
        max_iterations=state["max_iterations"],
        runtime=state["runtime"],
        errors=state["errors"],
        provider=(ctx.provider if ctx and ctx.provider else defaults.provider),
        model=(ctx.model if ctx and ctx.model else defaults.model),
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

    # If preview is active (static or server), restart it with new files
    if gateway.is_active(session_id):
        await gateway.preview_stop(session_id, req.files, state["runtime"])
        try:
            await gateway.preview_start(session_id, req.files, state["runtime"])
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
    await gateway.preview_stop(session_id, state["files"], state["runtime"])

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

    # Re-apply provider/model/key if the client sent them; otherwise keep the
    # selection from the original /generate call.
    if req.provider or req.model or req.client_id or req.api_key:
        state["llm_context"] = await build_llm_context(
            req.client_id, req.provider, req.model, req.api_key
        )

    asyncio.create_task(_run_iterate_task(session_id, state, queue, _session_backends.get(session_id, "langgraph")))

    return GenerateResponse(session_id=session_id)


@app.get("/history/{session_id}")
async def get_history(session_id: str) -> dict[str, Any]:
    """Get version history — from DB if available, fallback to in-memory."""
    async with async_session_factory() as db:
        db_versions = await get_versions(db, session_id)

    if db_versions:
        versions = [
            {
                "version": v.version_number,
                "timestamp": v.created_at.isoformat() if v.created_at else "",
                "description": v.description,
                "trigger": v.trigger,
                "is_current": v.is_current,
            }
            for v in db_versions
        ]
        return {"versions": versions}

    # Fallback to in-memory for backward compatibility
    history = _session_histories.get(session_id, [])
    versions = [
        {"version": h["version"], "timestamp": h.get("timestamp", ""), "description": h.get("description", f"Version {h['version']}"), "is_current": h.get("is_current", False), "trigger": "initial"}
        for h in history
    ]
    return {"versions": versions}


@app.post("/checkout/{session_id}/{version}")
async def checkout_version_endpoint(session_id: str, version: int) -> dict[str, Any]:
    """Restore a version — creates a new rollback version (non-destructive)."""
    async with async_session_factory() as db:
        db_versions = await get_versions(db, session_id)

        if db_versions:
            # DB-backed: non-destructive rollback (creates new version)
            rollback = await db_checkout_version(db, session_id, version)
            if not rollback:
                raise HTTPException(404, "Version not found")

            # Update in-memory state with restored files
            state = _session_states.get(session_id)
            if state:
                state["files"] = rollback.files_snapshot

                # Restart preview if active (static or server)
                if gateway.is_active(session_id):
                    await gateway.preview_stop(session_id, state["files"], state["runtime"])
                    try:
                        await gateway.preview_start(session_id, state["files"], state["runtime"])
                    except RuntimeError:
                        pass

                    queue = _event_queues.get(session_id)
                    if queue:
                        try:
                            queue.put_nowait({"event": "preview_reload"})
                        except asyncio.QueueFull:
                            pass

            return {
                "status": "checked_out",
                "version": rollback.version_number,
                "restored_from": version,
                "files": rollback.files_snapshot,
            }

    # Fallback to in-memory
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

    # If preview is active (static or server), restart it with new files
    if gateway.is_active(session_id):
        await gateway.preview_stop(session_id, state["files"], state["runtime"])
        try:
            await gateway.preview_start(session_id, state["files"], state["runtime"])
        except RuntimeError:
            pass

        queue = _event_queues.get(session_id)
        if queue:
            try:
                queue.put_nowait({"event": "preview_reload"})
            except asyncio.QueueFull:
                pass

    return {"status": "checked_out", "version": version, "files": state["files"]}


# ── Diff endpoint ─────────────────────────────────────────────────────────────


@app.get("/diff/{session_id}/{v1}/{v2}", response_model=DiffResponse)
async def diff_versions(session_id: str, v1: int, v2: int) -> DiffResponse:
    """Compute on-demand diff between two versions."""
    if v1 == v2:
        raise HTTPException(400, "Cannot diff a version against itself")

    async with async_session_factory() as db:
        version_1 = await get_version(db, session_id, v1)
        version_2 = await get_version(db, session_id, v2)

    # Fallback to in-memory if DB doesn't have them
    if not version_1 or not version_2:
        history = _session_histories.get(session_id, [])
        target_1 = next((h for h in history if h["version"] == v1), None)
        target_2 = next((h for h in history if h["version"] == v2), None)
        if not target_1 or not target_2:
            raise HTTPException(404, "One or both versions not found")
        files_v1 = target_1["files"]
        files_v2 = target_2["files"]
    else:
        files_v1 = version_1.files_snapshot
        files_v2 = version_2.files_snapshot

    result = compute_diff(files_v1, files_v2, v1, v2)

    return DiffResponse(
        v1=result.v1,
        v2=result.v2,
        changes=[
            FileDiffSchema(
                file=c.file,
                status=c.status.value,
                diff=c.diff,
                additions=c.additions,
                deletions=c.deletions,
            )
            for c in result.changes
        ],
        summary=result.summary,
    )


# ── Sessions list endpoint ────────────────────────────────────────────────────


@app.get("/sessions", response_model=SessionListResponse)
async def list_sessions() -> SessionListResponse:
    """List all sessions — from DB, plus any in-memory-only sessions."""
    from sqlalchemy import select

    sessions: list[SessionSummary] = []

    # DB sessions
    async with async_session_factory() as db:
        result = await db.execute(
            select(DBSession).order_by(DBSession.created_at.desc()).limit(50)
        )
        for s in result.scalars().all():
            sessions.append(SessionSummary(
                session_id=str(s.id),
                idea=s.idea[:100],
                runtime=s.runtime,
                backend=s.backend,
                status=s.status,
                created_at=s.created_at.isoformat() if s.created_at else "",
            ))

    # In-memory sessions not yet in DB
    db_ids = {s.session_id for s in sessions}
    for sid, state in _session_states.items():
        if sid not in db_ids:
            sessions.append(SessionSummary(
                session_id=sid,
                idea=state["idea"][:100],
                runtime=state["runtime"],
                backend=_session_backends.get(sid, "langgraph"),
                status=state["status"],
                created_at="",
            ))

    return SessionListResponse(sessions=sessions)


# ── Chat endpoints ────────────────────────────────────────────────────────────


@app.post("/chat/{session_id}", response_model=ChatResponse)
async def send_chat_message(session_id: str, req: ChatMessageRequest) -> ChatResponse:
    """Send a chat message and get an AI response. May trigger iteration."""
    from src.dev_agent.chat import generate_chat_response, store_message

    state = _session_states.get(session_id)
    if not state:
        raise HTTPException(404, "Session not found")

    # Resolve the LLM selection: prefer the request's, else the session's.
    if req.provider or req.model or req.client_id or req.api_key:
        ctx: LLMContext | None = await build_llm_context(
            req.client_id, req.provider, req.model, req.api_key
        )
        state["llm_context"] = ctx
    else:
        ctx = state.get("llm_context")

    async with async_session_factory() as db:
        # Store user message
        await store_message(db, session_id, "user", req.message)

        # Build context for the chat
        session_context = {
            "idea": state.get("idea", ""),
            "runtime": state.get("runtime", "auto"),
            "status": state.get("status", "unknown"),
            "files": state.get("files", {}),
        }

        # Generate AI response
        response_text, should_iterate = await generate_chat_response(
            db, session_id, req.message, session_context, ctx
        )

        # Store assistant response
        assistant_msg = await store_message(
            db, session_id, "assistant", response_text,
            metadata={"should_iterate": should_iterate},
        )

    # Emit chat event via SSE if queue exists
    queue = _event_queues.get(session_id)
    if queue:
        try:
            queue.put_nowait({
                "event": "chat_response",
                "message": response_text,
                "should_iterate": should_iterate,
            })
        except asyncio.QueueFull:
            pass

    # If iteration triggered and pipeline is ready, start it
    iteration_feedback = ""
    if should_iterate and state.get("status") == "done" and state.get("plan"):
        iteration_feedback = req.message
        # Kick off iterate pipeline
        await gateway.preview_stop(session_id, state["files"], state["runtime"])

        iter_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        _event_queues[session_id] = iter_queue
        state["status"] = "running"
        state["user_feedback"] = req.message
        state["iteration"] = 0
        state["test_report"] = None
        state["errors"] = []
        state["event_queue"] = iter_queue

        asyncio.create_task(
            _run_iterate_task(session_id, state, iter_queue, _session_backends.get(session_id, "langgraph"))
        )

    return ChatResponse(
        message=ChatMessageSchema(
            id=str(assistant_msg.id),
            role="assistant",
            content=response_text,
            created_at=assistant_msg.created_at.isoformat() if assistant_msg.created_at else "",
        ),
        should_iterate=should_iterate,
        iteration_feedback=iteration_feedback,
    )


@app.get("/chat/{session_id}", response_model=ChatHistoryResponse)
async def get_chat_history_endpoint(session_id: str) -> ChatHistoryResponse:
    """Retrieve full chat history for a session."""
    from src.dev_agent.chat import get_chat_history

    async with async_session_factory() as db:
        messages = await get_chat_history(db, session_id)

    return ChatHistoryResponse(
        session_id=session_id,
        messages=[
            ChatMessageSchema(
                id=str(m.id),
                role=m.role,
                content=m.content,
                created_at=m.created_at.isoformat() if m.created_at else "",
                metadata=m.metadata_ or {},
            )
            for m in messages
        ],
    )


# ── Memory endpoints ──────────────────────────────────────────────────────────


@app.get("/memory", response_model=MemoryListResponse)
async def list_memories() -> MemoryListResponse:
    """List all stored cross-session memories."""
    from src.dev_agent.memory.memory_store import get_all_memories

    async with async_session_factory() as db:
        memories = await get_all_memories(db)

    return MemoryListResponse(
        memories=[
            MemorySchema(
                id=str(m.id),
                category=m.category,
                key=m.key,
                value=m.value,
                relevance_score=m.relevance_score,
                access_count=m.access_count,
                created_at=m.created_at.isoformat() if m.created_at else "",
            )
            for m in memories
        ],
        total=len(memories),
    )


@app.post("/memory", response_model=MemorySchema)
async def create_memory(req: MemoryCreateRequest) -> MemorySchema:
    """Manually add a memory entry."""
    from src.dev_agent.memory.memory_store import store_memory

    async with async_session_factory() as db:
        mem = await store_memory(db, req.category, req.key, req.value)

    return MemorySchema(
        id=str(mem.id),
        category=mem.category,
        key=mem.key,
        value=mem.value,
        relevance_score=mem.relevance_score,
        access_count=mem.access_count,
        created_at=mem.created_at.isoformat() if mem.created_at else "",
    )


@app.delete("/memory/{memory_id}")
async def delete_memory_endpoint(memory_id: str) -> dict[str, str]:
    """Delete a specific memory."""
    from src.dev_agent.memory.memory_store import delete_memory

    async with async_session_factory() as db:
        deleted = await delete_memory(db, memory_id)

    if not deleted:
        raise HTTPException(404, "Memory not found")
    return {"status": "deleted"}


@app.post("/memory/clear")
async def clear_memories() -> dict[str, Any]:
    """Clear all stored memories."""
    from src.dev_agent.memory.memory_store import clear_all_memories

    async with async_session_factory() as db:
        count = await clear_all_memories(db)

    return {"status": "cleared", "deleted_count": count}


# ── Provider / BYOK key endpoints ─────────────────────────────────────────────


@app.get("/providers", response_model=ProvidersResponse)
async def list_providers(client_id: str | None = None) -> ProvidersResponse:
    """Public provider/model registry for the UI dropdown.

    When ``client_id`` is given, marks which providers this browser has saved a
    (encrypted) key for. Never returns key material.
    """
    from src.dev_agent.security import keyvault

    registry = config.public_registry()
    defaults = config.get_default()

    configured: set[str] = set()
    if client_id:
        try:
            from src.dev_agent.db.keys_store import list_configured_providers

            async with async_session_factory() as db:
                configured = await list_configured_providers(db, client_id)
        except Exception as exc:
            logger.warning("Could not list configured providers: %s", exc)

    providers = [
        ProviderInfo(
            id=str(p["id"]),
            label=str(p["label"]),
            byok=bool(p["byok"]),
            default_model=str(p["default_model"]),
            models=p["models"],  # type: ignore[arg-type]
            configured=p["id"] in configured,
        )
        for p in registry
    ]
    return ProvidersResponse(
        providers=providers,
        default_provider=defaults.provider,
        default_model=defaults.model,
        encryption_enabled=keyvault.is_configured(),
    )


@app.put("/providers/{provider}/key")
async def save_provider_key(provider: str, req: ProviderKeyRequest) -> dict[str, str]:
    """Encrypt and persist a user's API key for a provider (BYOK)."""
    from src.dev_agent.security import keyvault

    if config.load_model_config().provider(provider) is None:
        raise HTTPException(404, f"Unknown provider '{provider}'")
    if not keyvault.is_configured():
        raise HTTPException(
            503,
            "Key storage is disabled — set ENCRYPTION_KEY on the server to enable BYOK.",
        )

    from src.dev_agent.db.keys_store import upsert_provider_key

    async with async_session_factory() as db:
        await upsert_provider_key(db, req.client_id, provider, req.api_key)
    return {"status": "saved", "provider": provider}


@app.delete("/providers/{provider}/key")
async def remove_provider_key(provider: str, client_id: str) -> dict[str, str]:
    """Delete a user's saved key for a provider."""
    from src.dev_agent.db.keys_store import delete_provider_key

    async with async_session_factory() as db:
        deleted = await delete_provider_key(db, client_id, provider)
    if not deleted:
        raise HTTPException(404, "No saved key for that provider")
    return {"status": "deleted", "provider": provider}


# ── File retrieval (fallback for SSE delivery issues) ─────────────────────────


@app.get("/files/{session_id}")
async def get_files(session_id: str) -> dict[str, Any]:
    """Return generated files for a session (fallback if SSE delivery fails)."""
    state = _session_states.get(session_id)
    if not state:
        raise HTTPException(404, "Session not found")
    return {"files": state.get("files", {}), "runtime": state.get("runtime", "")}


# ── Preview endpoints ─────────────────────────────────────────────────────────


@app.post("/preview/{session_id}/start", response_model=PreviewStartResponse)
async def preview_start(session_id: str) -> PreviewStartResponse:
    state = _session_states.get(session_id)
    if not state:
        raise HTTPException(404, "Session not found")
    if state["status"] != "done":
        raise HTTPException(400, "Pipeline not complete — cannot start preview")

    try:
        info = await gateway.preview_start(
            session_id, state["files"], state["runtime"], state.get("event_queue")
        )
    except RuntimeError as exc:
        # Surface the real failure (captured stderr) so the UI can show it.
        raise HTTPException(503, str(exc)) from None

    return PreviewStartResponse(
        port=int(info.get("port", 0)),
        url=str(info["url"]),
        status=str(info["status"]),
        mode=str(info.get("mode", "server")),
    )


@app.post("/preview/{session_id}/stop")
async def preview_stop(session_id: str) -> dict[str, str]:
    state = _session_states.get(session_id)
    if not state:
        raise HTTPException(404, "Session not found")

    await gateway.preview_stop(session_id, state["files"], state["runtime"])
    return {"status": "stopped"}


@app.api_route(
    "/preview/{session_id}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
)
async def preview_proxy(session_id: str, path: str, request: Request) -> Response:
    """Serve the session's preview via the execution gateway (static or server)."""
    body = await request.body()
    return await gateway.serve(session_id, path, request.method, body, request.url.query)


# ── Background task ───────────────────────────────────────────────────────────


async def _run_pipeline_task(
    session_id: str,
    initial_state: DevPipelineState,
    queue: asyncio.Queue[dict[str, Any]],
    backend_name: str = "langgraph",
) -> None:
    """Run the planner, emit plan_ready, then wait for user approval."""
    try:
        plan_result = await planner_node(initial_state)
        state = _session_states[session_id]
        state["plan"] = plan_result["plan"]
        state["runtime"] = plan_result["runtime"]
        state["current_agent"] = "planner"
        state["status"] = "awaiting_approval"

        plan = plan_result["plan"]
        await queue.put({
            "event": "plan_ready",
            "data": {
                "app_name": plan.app_name,
                "runtime": plan.runtime,
                "tech_stack": plan.tech_stack,
                "architecture": plan.architecture_notes,
                "files": plan.estimated_files,
                "entry_point": plan.entry_point,
                "tasks": [{"id": t.id, "title": t.title, "description": t.description} for t in plan.tasks],
                "ui_design_notes": plan.ui_design_notes,
            },
        })

    except Exception as exc:
        logger.exception("Planner failed for session %s", session_id)
        _session_states[session_id]["status"] = "error"
        _session_states[session_id]["errors"].append(str(exc))
        await queue.put({"event": "error", "message": str(exc)})


async def _run_build_task(
    session_id: str,
    state: DevPipelineState,
    queue: asyncio.Queue[dict[str, Any]],
    backend_name: str = "langgraph",
) -> None:
    """Run the build pipeline (developer → designer → tester → reviewer) after plan approval."""
    try:
        backend = get_backend(backend_name)
        final_state = await backend.run_iterate(state, queue)

        _session_states[session_id] = final_state
        _session_states[session_id]["status"] = "done"

        if session_id not in _session_histories:
            _session_histories[session_id] = []
        _session_histories[session_id].append({
            "version": 1,
            "description": f"Initial Build ({backend_name})",
            "files": dict(final_state.get("files", {})),
            "is_current": True,
        })

        try:
            async with async_session_factory() as db:
                from sqlalchemy import update
                await db.execute(
                    update(DBSession)
                    .where(DBSession.id == uuid.UUID(session_id))
                    .values(
                        status="done",
                        plan=final_state["plan"].model_dump() if final_state.get("plan") else None,
                        errors=final_state.get("errors", []),
                        runtime=final_state["runtime"],
                    )
                )
                await db.commit()
                await create_version(
                    db=db,
                    session_id=session_id,
                    version_number=1,
                    description=f"Initial Build ({backend_name})",
                    trigger="initial",
                    files_snapshot=dict(final_state.get("files", {})),
                    metadata={
                        "review_notes": final_state.get("review_notes", []),
                        "test_report": final_state["test_report"].model_dump() if final_state.get("test_report") else None,
                    },
                )
        except Exception as db_exc:
            logger.warning("Failed to persist to DB (in-memory still works): %s", db_exc)

        try:
            await cache_session_state(session_id, final_state)
        except Exception:
            pass

        try:
            from src.dev_agent.memory.memory_manager import extract_and_store_memories
            async with async_session_factory() as db:
                await extract_and_store_memories(
                    db=db,
                    idea=state["idea"],
                    runtime=final_state["runtime"],
                    files=final_state.get("files", {}),
                    review_notes=final_state.get("review_notes", []),
                    test_report=final_state["test_report"].model_dump() if final_state.get("test_report") else None,
                    ctx=final_state.get("llm_context"),
                )
        except Exception as mem_exc:
            logger.warning("Memory extraction failed (non-critical): %s", mem_exc)

        file_count = len(final_state.get("files", {}))
        logger.info("pipeline_done: emitting %d files for session %s", file_count, session_id)

        await queue.put({
            "event": "pipeline_done",
            "files": final_state["files"],
            "runtime": final_state["runtime"],
            "review_notes": final_state.get("review_notes", []),
            "backend": backend_name,
        })

    except Exception as exc:
        logger.exception("Build pipeline failed for session %s", session_id)
        _session_states[session_id]["status"] = "error"
        _session_states[session_id]["errors"].append(str(exc))
        await queue.put({"event": "error", "message": str(exc)})


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

        # Record iteration in history (in-memory)
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

        # Persist version to PostgreSQL
        try:
            async with async_session_factory() as db:
                next_num = await get_next_version_number(db, session_id)
                await create_version(
                    db=db,
                    session_id=session_id,
                    version_number=next_num,
                    description=f"Iteration {next_num - 1} ({backend_name})",
                    trigger="iteration",
                    files_snapshot=dict(final_state.get("files", {})),
                    metadata={
                        "review_notes": final_state.get("review_notes", []),
                        "test_report": final_state["test_report"].model_dump() if final_state.get("test_report") else None,
                        "user_feedback": state.get("user_feedback", ""),
                    },
                )
        except Exception as db_exc:
            logger.warning("Failed to persist iteration to DB: %s", db_exc)

        # Cache updated state
        try:
            await cache_session_state(session_id, final_state)
        except Exception:
            pass

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
