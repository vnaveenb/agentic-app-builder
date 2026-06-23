"""Redis cache — session state caching and pub/sub for SSE."""

from __future__ import annotations

import json
import os
from typing import Any

import redis.asyncio as redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

_pool: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    """Get or create the Redis connection pool."""
    global _pool
    if _pool is None:
        _pool = redis.from_url(REDIS_URL, decode_responses=True)
    return _pool


async def close_redis() -> None:
    """Close the Redis connection pool."""
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None


async def cache_session_state(session_id: str, state: dict[str, Any]) -> None:
    """Cache session state in Redis (excluding non-serializable fields)."""
    r = await get_redis()
    # Strip non-serializable fields
    serializable = {
        k: v for k, v in state.items()
        if k not in ("event_queue",) and v is not None
    }
    # Handle Pydantic models
    for key in ("plan", "test_report", "preview"):
        if key in serializable and hasattr(serializable[key], "model_dump"):
            serializable[key] = serializable[key].model_dump()
    await r.set(f"session:{session_id}", json.dumps(serializable), ex=86400)  # 24h TTL


async def get_cached_state(session_id: str) -> dict[str, Any] | None:
    """Retrieve cached session state from Redis."""
    r = await get_redis()
    data = await r.get(f"session:{session_id}")
    if data:
        return json.loads(data)
    return None


async def delete_cached_state(session_id: str) -> None:
    """Remove session state from cache."""
    r = await get_redis()
    await r.delete(f"session:{session_id}")


async def health_check() -> bool:
    """Check Redis connectivity."""
    try:
        r = await get_redis()
        return await r.ping()
    except Exception:
        return False
