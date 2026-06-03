"""Abstract base for pipeline backends (LangGraph, CrewAI, etc.)."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from src.dev_agent.pipeline.state import DevPipelineState


class PipelineBackend(ABC):
    """Interface that every orchestration backend must implement."""

    @abstractmethod
    async def run(
        self,
        state: DevPipelineState,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> DevPipelineState:
        """Execute the full pipeline (planner → developer → tester → reviewer)."""

    @abstractmethod
    async def run_iterate(
        self,
        state: DevPipelineState,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> DevPipelineState:
        """Execute the iterate pipeline (developer → tester → reviewer), skipping planner."""
