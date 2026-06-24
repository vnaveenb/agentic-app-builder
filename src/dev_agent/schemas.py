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


class LLMSelection(BaseModel):
    """Optional per-request LLM provider/model override + BYOK identity.

    ``client_id`` is the browser-minted id used to look up saved (encrypted) keys.
    ``api_key`` is an optional transient key (test-without-saving); saved keys are
    resolved server-side from client_id + provider.
    """

    client_id: str | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, max_length=50)
    model: str | None = Field(default=None, max_length=120)
    api_key: str | None = Field(default=None, max_length=400)


class GenerateRequest(LLMSelection):
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
    provider: str = ""
    model: str = ""


class PreviewStartResponse(BaseModel):
    port: int = 0  # 0 for static previews (served directly, no subprocess)
    url: str
    status: str
    mode: str = "server"  # "static" | "server"


class IterateRequest(LLMSelection):
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


class ChatMessageRequest(LLMSelection):
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


# ── Provider / BYOK models ────────────────────────────────────────────────────


class ModelInfo(BaseModel):
    id: str
    label: str
    default: bool = False


class ProviderInfo(BaseModel):
    id: str
    label: str
    byok: bool
    default_model: str = ""
    models: list[ModelInfo] = Field(default_factory=list)
    configured: bool = False  # True if this client has a saved key for the provider


class ProvidersResponse(BaseModel):
    providers: list[ProviderInfo]
    default_provider: str
    default_model: str
    encryption_enabled: bool


class ProviderKeyRequest(BaseModel):
    client_id: str = Field(..., min_length=1, max_length=64)
    api_key: str = Field(..., min_length=1, max_length=400)
