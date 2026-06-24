"""Encrypt/decrypt user-supplied (BYOK) API keys at rest.

Uses Fernet (AES-128-CBC + HMAC) keyed by the ENCRYPTION_KEY environment
variable, which must be a urlsafe-base64-encoded 32-byte key.

Key resolution order:
1. ENCRYPTION_KEY env var (production override)
2. Persisted key file at DATA_DIR/.encryption_key (auto-generated on first run)
3. If neither exists, generate a new key and persist it

This means BYOK "just works" out of the box in Docker — no manual key setup
required. For production, set ENCRYPTION_KEY explicitly in the environment.
"""

from __future__ import annotations

import functools
import logging
import os
import pathlib

from cryptography.fernet import Fernet, InvalidToken

_ENV_VAR = "ENCRYPTION_KEY"
_DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "/app/data"))
_KEY_FILE = _DATA_DIR / ".encryption_key"

logger = logging.getLogger(__name__)


class EncryptionUnavailableError(RuntimeError):
    """Raised when BYOK encryption is requested but ENCRYPTION_KEY is not set."""


def _resolve_key() -> str:
    """Resolve the encryption key: env var → persisted file → auto-generate."""
    key = os.environ.get(_ENV_VAR)
    if key:
        return key

    if _KEY_FILE.exists():
        key = _KEY_FILE.read_text().strip()
        if key:
            os.environ[_ENV_VAR] = key
            logger.info("Loaded encryption key from %s", _KEY_FILE)
            return key

    key = Fernet.generate_key().decode()
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _KEY_FILE.write_text(key)
        _KEY_FILE.chmod(0o600)
        logger.info("Generated and persisted new encryption key to %s", _KEY_FILE)
    except OSError as exc:
        logger.warning("Could not persist encryption key (%s) — using ephemeral key", exc)

    os.environ[_ENV_VAR] = key
    return key


def is_configured() -> bool:
    """True if an ENCRYPTION_KEY can be resolved (BYOK storage is available)."""
    try:
        _resolve_key()
        return True
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = _resolve_key()
    return Fernet(key.encode())


def encrypt(plaintext: str) -> bytes:
    """Encrypt a plaintext API key. Raises EncryptionUnavailableError if no key."""
    return _fernet().encrypt(plaintext.encode())


def decrypt(token: bytes) -> str:
    """Decrypt a stored key token. Raises InvalidToken if the key/ciphertext is bad."""
    return _fernet().decrypt(token).decode()


__all__ = [
    "EncryptionUnavailableError",
    "InvalidToken",
    "is_configured",
    "encrypt",
    "decrypt",
]
