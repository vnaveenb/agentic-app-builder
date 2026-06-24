"""Seed the database with the default admin user (linked to Firebase Auth)."""

from __future__ import annotations

import logging

from sqlalchemy import select

from src.dev_agent.auth.service import (
    create_firebase_user,
    get_user_by_email,
    set_admin_claim,
)
from src.dev_agent.db.database import async_session_factory
from src.dev_agent.db.models import User

logger = logging.getLogger(__name__)

_ADMIN_EMAIL = "admin@naveenb.net"


async def seed_admin_user() -> None:
    """Ensure admin user exists in both Firebase and local DB."""
    # Check local DB first
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.email == _ADMIN_EMAIL))
        existing = result.scalar_one_or_none()
        if existing:
            logger.info("Admin user already exists in DB, skipping seed")
            return

    # Ensure Firebase user exists
    try:
        fb_user = get_user_by_email(_ADMIN_EMAIL)
        if not fb_user:
            fb_user = create_firebase_user(_ADMIN_EMAIL)
            logger.info("Created Firebase user for admin: %s", _ADMIN_EMAIL)

        set_admin_claim(fb_user.uid, is_admin=True)
    except Exception as exc:
        logger.warning(
            "Firebase seed skipped (SDK not configured or unreachable): %s — "
            "admin will be created on first login",
            exc,
        )
        return

    # Create local DB record
    async with async_session_factory() as db:
        admin = User(
            firebase_uid=fb_user.uid,
            email=_ADMIN_EMAIL,
            is_admin=True,
        )
        db.add(admin)
        await db.commit()
        logger.info("Admin user seeded: %s (firebase_uid=%s)", _ADMIN_EMAIL, fb_user.uid)
