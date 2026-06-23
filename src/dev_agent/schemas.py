"""HTTP boundary Pydantic models — Project 7: AI Dev Agent."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    project: str
    provider: str
    runtimes: list[str]
    database: str = "connected"
    redis: str = "connected"


class GenerateRequest(BaseModel):
    idea: str
    runtime: str = "auto"
    max_iterations: int = Field(default=2, ge=1, le=5)
    backend: str = Field(default="langgraph", pattern="^(langgraph|crewai)$")


class GenerateResponse(BaseModel):
    session_id: str


class StatusResponse(BaseModel):
    session_id: str
    status: str
    current_agent: str
    iteration: int
    max_iterations: int
    runtime: str
    errors: list[str]


class PreviewStartResponse(BaseModel):
    port: int
    url: str
    status: str


class IterateRequest(BaseModel):
    feedback: str = Field(..., min_length=1, max_length=2000)


class UpdateFilesRequest(BaseModel):
    files: dict[str, str]


# ── Version & Diff models ─────────────────────────────────────────────────────


class VersionSchema(BaseModel):
    version: int
    timestamp: str = ""
    description: str
    trigger: str = "initial"
    is_current: bool = False


class VersionResponse(BaseModel):
    versions: list[VersionSchema]


class FileDiffSchema(BaseModel):
    file: str
    status: str  # "added", "modified", "deleted"
    diff: str = ""
    additions: int = 0
    deletions: int = 0


class DiffResponse(BaseModel):
    v1: int
    v2: int
    changes: list[FileDiffSchema]
    summary: dict[str, int]


# ── Session list models ───────────────────────────────────────────────────────


class SessionSummary(BaseModel):
    session_id: str
    idea: str
    runtime: str
    backend: str = "langgraph"
    status: str
    created_at: str = ""


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]


# ── Chat models ───────────────────────────────────────────────────────────────


class ChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class ChatMessageSchema(BaseModel):
    id: str
    role: str  # "user", "assistant", "system"
    content: str
    created_at: str = ""
    metadata: dict = Field(default_factory=dict)


class ChatResponse(BaseModel):
    message: ChatMessageSchema
    should_iterate: bool = False
    iteration_feedback: str = ""


class ChatHistoryResponse(BaseModel):
    session_id: str
    messages: list[ChatMessageSchema]


# ── Memory models ─────────────────────────────────────────────────────────────


class MemorySchema(BaseModel):
    id: str
    category: str
    key: str
    value: str
    relevance_score: float = 1.0
    access_count: int = 0
    created_at: str = ""


class MemoryListResponse(BaseModel):
    memories: list[MemorySchema]
    total: int


class MemoryCreateRequest(BaseModel):
    category: str = Field(..., pattern="^(preference|pattern|project_summary)$")
    key: str = Field(..., min_length=1, max_length=200)
    value: str = Field(..., min_length=1, max_length=500)
