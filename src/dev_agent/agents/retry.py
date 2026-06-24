"""Shared retry logic and task emission helpers for agent nodes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Errors that are transient and worth retrying
_RETRYABLE_KEYWORDS = ("503", "429", "unavailable", "overloaded", "rate limit", "high demand", "quota", "resource_exhausted")


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception is transient (503, 429, rate limit, etc.)."""
    err_str = str(exc).lower()
    return any(kw in err_str for kw in _RETRYABLE_KEYWORDS)


async def retry_llm_call(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 4,
    base_delay: float = 3.0,
    max_delay: float = 30.0,
    agent_name: str = "agent",
    queue: asyncio.Queue | None = None,
    task_id: int | None = None,
    task_text: str | None = None,
    **kwargs: Any,
) -> Any:
    """Retry an async LLM call with exponential backoff on transient errors.

    Args:
        fn: Async callable to retry (e.g., llm.ainvoke)
        *args: Positional arguments for fn
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay between retries (doubles each time)
        max_delay: Maximum delay cap
        agent_name: Name for logging context
        queue: Optional SSE event queue for emitting retry status
        task_id: Optional task ID to update during retries
        task_text: Original task text (used to build retry message)
        **kwargs: Keyword arguments for fn

    Returns:
        The result of fn(*args, **kwargs)

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None
    total_attempts = max_retries + 1

    for attempt in range(total_attempts):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc):
                raise

            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                logger.warning(
                    "[%s] Transient error (attempt %d/%d), retrying in %.1fs: %s",
                    agent_name, attempt + 1, total_attempts, delay, str(exc)[:200],
                )
                # Emit retry status via SSE
                if queue and task_id is not None and task_text:
                    await queue.put({
                        "event": "task_update",
                        "agent": agent_name,
                        "task_id": task_id,
                        "text": f"{task_text} (retry {attempt + 1}/{max_retries}, {delay:.0f}s...)",
                    })
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "[%s] All %d attempts exhausted. Last error: %s",
                    agent_name, total_attempts, str(exc)[:200],
                )

    raise last_exc  # type: ignore[misc]


# ─── Task Emission Helpers ─────────────────────────────────────────────

PLANNER_TASKS = [
    "Analyzing requirements",
    "Designing architecture",
    "Defining file structure",
]

DEVELOPER_TASKS = [
    "Preparing prompt",
    "Generating code",
    "Parsing & validating output",
]

TESTER_TASKS = [
    "Running sandbox tests",
    "Performing static analysis",
    "Merging results",
]

REVIEWER_TASKS = [
    "Reviewing code quality",
    "Applying improvements",
    "Generating review notes",
]

DESIGNER_TASKS = [
    "Analyzing UI quality",
    "Enhancing visual design",
    "Finalizing styled files",
]


async def emit_tasks(queue: asyncio.Queue | None, agent: str, tasks: list[str]) -> None:
    """Emit a tasks event with the full task list for an agent."""
    if queue:
        await queue.put({
            "event": "tasks",
            "agent": agent,
            "tasks": [{"id": i, "text": t} for i, t in enumerate(tasks)],
        })


async def emit_task_done(queue: asyncio.Queue | None, agent: str, task_id: int) -> None:
    """Emit a task_done event marking a specific task as completed."""
    if queue:
        await queue.put({
            "event": "task_done",
            "agent": agent,
            "task_id": task_id,
        })
