"""Async database engine and session factory — PostgreSQL via asyncpg."""

from __future__ import annotations

import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://devagent:devagent@localhost:5432/devagent",
)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncSession:
    """Create a new async database session."""
    async with async_session_factory() as session:
        return session


async def _apply_migrations(conn) -> None:  # type: ignore[no-untyped-def]
    """Add columns that create_all() can't add to pre-existing tables."""
    await conn.execute(text(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)"
    ))
    logger.info("Schema migrations applied")


async def init_db() -> None:
    """Create all tables (for development — use Alembic in production)."""
    from src.dev_agent.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_migrations(conn)


async def close_db() -> None:
    """Dispose of the engine connection pool."""
    await engine.dispose()
