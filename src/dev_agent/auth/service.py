"""Authentication service — Firebase Auth token verification."""

from __future__ import annotations

import json
import logging
import os

import firebase_admin
from firebase_admin import auth as firebase_auth, credentials

logger = logging.getLogger(__name__)

_app: firebase_admin.App | None = None


def _init_firebase() -> firebase_admin.App:
    global _app
    if _app is not None:
        return _app

    cred_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
    cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")

    if cred_json:
        cred = credentials.Certificate(json.loads(cred_json))
        logger.info("Firebase Admin SDK: using inline JSON credentials")
    elif cred_path and os.path.isfile(cred_path):
        cred = credentials.Certificate(cred_path)
        logger.info("Firebase Admin SDK: using service account file %s", cred_path)
    elif cred_path:
        raise RuntimeError(
            f"FIREBASE_SERVICE_ACCOUNT is set to '{cred_path}' but file does not exist. "
            "Either mount the file into the container or set FIREBASE_SERVICE_ACCOUNT_JSON."
        )
    else:
        raise RuntimeError(
            "Firebase Auth not configured. Set FIREBASE_SERVICE_ACCOUNT (file path) or "
            "FIREBASE_SERVICE_ACCOUNT_JSON (inline JSON) in your .env file."
        )

    _app = firebase_admin.initialize_app(cred)
    logger.info("Firebase Admin SDK initialized (project: %s)", _app.project_id)
    return _app


def verify_firebase_token(id_token: str) -> dict:
    """Verify a Firebase ID token and return the decoded claims.

    Returns dict with at minimum: uid, email, email_verified.
    Raises ValueError or firebase_admin exceptions on invalid token.
    """
    _init_firebase()
    decoded = firebase_auth.verify_id_token(id_token)
    return decoded


def get_user_by_email(email: str) -> firebase_auth.UserRecord | None:
    """Lookup a Firebase user by email."""
    _init_firebase()
    try:
        return firebase_auth.get_user_by_email(email)
    except firebase_auth.UserNotFoundError:
        return None


def create_firebase_user(email: str) -> firebase_auth.UserRecord:
    """Create a Firebase user (for seeding admin)."""
    _init_firebase()
    return firebase_auth.create_user(email=email, email_verified=True)


def set_admin_claim(uid: str, is_admin: bool = True) -> None:
    """Set custom claims on a Firebase user (admin role)."""
    _init_firebase()
    firebase_auth.set_custom_user_claims(uid, {"admin": is_admin})
