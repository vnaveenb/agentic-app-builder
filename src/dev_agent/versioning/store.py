"""Version store — CRUD operations for version persistence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.dev_agent.db.models import Version


async def create_version(
    db: AsyncSession,
    session_id: str,
    version_number: int,
    description: str,
    trigger: str,
    files_snapshot: dict[str, str],
    metadata: dict[str, Any] | None = None,
) -> Version:
    """Create a new version snapshot and mark it as current."""
    # Mark all existing versions for this session as not current
    await db.execute(
        update(Version)
        .where(Version.session_id == uuid.UUID(session_id))
        .values(is_current=False)
    )

    version = Version(
        id=uuid.uuid4(),
        session_id=uuid.UUID(session_id),
        version_number=version_number,
        description=description,
        trigger=trigger,
        files_snapshot=files_snapshot,
        metadata_=metadata or {},
        is_current=True,
    )
    db.add(version)
    await db.commit()
    await db.refresh(version)
    return version


async def get_versions(db: AsyncSession, session_id: str) -> list[Version]:
    """Get all versions for a session, ordered by version number."""
    result = await db.execute(
        select(Version)
        .where(Version.session_id == uuid.UUID(session_id))
        .order_by(Version.version_number)
    )
    return list(result.scalars().all())


async def get_version(db: AsyncSession, session_id: str, version_number: int) -> Version | None:
    """Get a specific version by session and version number."""
    result = await db.execute(
        select(Version)
        .where(
            Version.session_id == uuid.UUID(session_id),
            Version.version_number == version_number,
        )
    )
    return result.scalar_one_or_none()


async def get_current_version(db: AsyncSession, session_id: str) -> Version | None:
    """Get the currently active version for a session."""
    result = await db.execute(
        select(Version)
        .where(
            Version.session_id == uuid.UUID(session_id),
            Version.is_current == True,  # noqa: E712
        )
    )
    return result.scalar_one_or_none()


async def get_next_version_number(db: AsyncSession, session_id: str) -> int:
    """Get the next version number for a session."""
    versions = await get_versions(db, session_id)
    if not versions:
        return 1
    return versions[-1].version_number + 1


async def checkout_version(
    db: AsyncSession,
    session_id: str,
    version_number: int,
) -> Version | None:
    """Checkout a version — creates a new rollback version with old files.

    Non-destructive: never deletes history. Rollback creates a new version
    pointing to the target version's files (same pattern as Replit).
    """
    target = await get_version(db, session_id, version_number)
    if not target:
        return None

    next_num = await get_next_version_number(db, session_id)

    # Create a new rollback version with the target's files
    rollback = await create_version(
        db=db,
        session_id=session_id,
        version_number=next_num,
        description=f"Restored to v{version_number}",
        trigger="rollback",
        files_snapshot=target.files_snapshot,
        metadata={"restored_from": version_number},
    )
    return rollback
