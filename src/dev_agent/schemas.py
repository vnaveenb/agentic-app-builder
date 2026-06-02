"""HTTP boundary Pydantic models — Project 7: AI Dev Agent."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    project: str
    provider: str
    runtimes: list[str]


class GenerateRequest(BaseModel):
    idea: str
    runtime: str = "auto"
    max_iterations: int = Field(default=2, ge=1, le=5)


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
