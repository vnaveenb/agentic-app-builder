"""Unit tests for the retry utility."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.dev_agent.agents.retry import (
    _is_retryable,
    emit_task_done,
    emit_tasks,
    retry_llm_call,
)


class TestIsRetryable:
    """Tests for _is_retryable error detection."""

    def test_503_is_retryable(self):
        exc = Exception("503 Service Unavailable")
        assert _is_retryable(exc) is True

    def test_429_is_retryable(self):
        exc = Exception("429 Too Many Requests")
        assert _is_retryable(exc) is True

    def test_overloaded_is_retryable(self):
        exc = Exception("The model is overloaded. Please try again later.")
        assert _is_retryable(exc) is True

    def test_rate_limit_is_retryable(self):
        exc = Exception("rate limit exceeded for this resource")
        assert _is_retryable(exc) is True

    def test_resource_exhausted_is_retryable(self):
        exc = Exception("RESOURCE_EXHAUSTED: quota exceeded")
        assert _is_retryable(exc) is True

    def test_400_not_retryable(self):
        exc = Exception("400 Bad Request: invalid schema")
        assert _is_retryable(exc) is False

    def test_auth_error_not_retryable(self):
        exc = Exception("401 Unauthorized: invalid API key")
        assert _is_retryable(exc) is False

    def test_generic_error_not_retryable(self):
        exc = ValueError("unexpected field 'foo'")
        assert _is_retryable(exc) is False


class TestRetryLlmCall:
    """Tests for retry_llm_call with exponential backoff."""

    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        fn = AsyncMock(return_value="result")
        result = await retry_llm_call(fn, "prompt", base_delay=0.01)
        assert result == "result"
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_503_then_succeeds(self):
        fn = AsyncMock(
            side_effect=[Exception("503 Service Unavailable"), "result"]
        )
        result = await retry_llm_call(fn, "prompt", base_delay=0.01, max_retries=3)
        assert result == "result"
        assert fn.call_count == 2

    @pytest.mark.asyncio
    async def test_non_retryable_fails_immediately(self):
        fn = AsyncMock(side_effect=Exception("400 Bad Request"))
        with pytest.raises(Exception, match="400 Bad Request"):
            await retry_llm_call(fn, "prompt", base_delay=0.01)
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_exhausts_retries_then_raises(self):
        fn = AsyncMock(side_effect=Exception("503 Service Unavailable"))
        with pytest.raises(Exception, match="503"):
            await retry_llm_call(fn, "prompt", base_delay=0.01, max_retries=2)
        assert fn.call_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_emits_task_update_on_retry(self):
        fn = AsyncMock(
            side_effect=[Exception("503 overloaded"), "ok"]
        )
        queue: asyncio.Queue = asyncio.Queue()
        await retry_llm_call(
            fn, "prompt",
            base_delay=0.01,
            queue=queue,
            agent_name="planner",
            task_id=1,
            task_text="Designing architecture",
        )
        # Should have emitted a task_update event
        event = await queue.get()
        assert event["event"] == "task_update"
        assert event["agent"] == "planner"
        assert event["task_id"] == 1
        assert "retry 1/" in event["text"]


class TestTaskEmission:
    """Tests for emit_tasks and emit_task_done helpers."""

    @pytest.mark.asyncio
    async def test_emit_tasks(self):
        queue: asyncio.Queue = asyncio.Queue()
        await emit_tasks(queue, "planner", ["Task A", "Task B", "Task C"])
        event = await queue.get()
        assert event["event"] == "tasks"
        assert event["agent"] == "planner"
        assert len(event["tasks"]) == 3
        assert event["tasks"][0] == {"id": 0, "text": "Task A"}

    @pytest.mark.asyncio
    async def test_emit_task_done(self):
        queue: asyncio.Queue = asyncio.Queue()
        await emit_task_done(queue, "developer", 2)
        event = await queue.get()
        assert event["event"] == "task_done"
        assert event["agent"] == "developer"
        assert event["task_id"] == 2

    @pytest.mark.asyncio
    async def test_emit_with_none_queue(self):
        # Should not raise
        await emit_tasks(None, "planner", ["Task A"])
        await emit_task_done(None, "planner", 0)
