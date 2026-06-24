"""CRUD for user-supplied (BYOK) provider API keys, encrypted at rest.

Keys are scoped to a browser-minted ``client_id``. Plaintext keys are encrypted
via src/dev_agent/security/keyvault.py before being persisted, and decrypted only
when an LLM call needs them.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.dev_agent.db.models import ProviderKey
from src.dev_agent.security import keyvault

logger = logging.getLogger(__name__)


async def upsert_provider_key(
    db: AsyncSession, client_id: str, provider: str, api_key: str
) -> None:
    """Encrypt and store (or replace) a user's API key for a provider."""
    encrypted = keyvault.encrypt(api_key)
    existing = await db.execute(
        select(ProviderKey).where(
            ProviderKey.client_id == client_id, ProviderKey.provider == provider
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        row.encrypted_key = encrypted
    else:
        db.add(
            ProviderKey(
                id=uuid.uuid4(),
                client_id=client_id,
                provider=provider,
                encrypted_key=encrypted,
            )
        )
    await db.commit()


async def get_provider_key(
    db: AsyncSession, client_id: str, provider: str
) -> str | None:
    """Return the decrypted API key for (client_id, provider), or None."""
    result = await db.execute(
        select(ProviderKey).where(
            ProviderKey.client_id == client_id, ProviderKey.provider == provider
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    try:
        return keyvault.decrypt(row.encrypted_key)
    except Exception as exc:  # InvalidToken / EncryptionUnavailableError
        logger.warning("Failed to decrypt stored key for provider %s: %s", provider, exc)
        return None


async def delete_provider_key(
    db: AsyncSession, client_id: str, provider: str
) -> bool:
    """Delete a stored key. Returns True if a row was removed."""
    result = await db.execute(
        delete(ProviderKey).where(
            ProviderKey.client_id == client_id, ProviderKey.provider == provider
        )
    )
    await db.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def list_configured_providers(db: AsyncSession, client_id: str) -> set[str]:
    """Return the set of provider ids this client has saved a key for."""
    result = await db.execute(
        select(ProviderKey.provider).where(ProviderKey.client_id == client_id)
    )
    return {row[0] for row in result.all()}


__all__ = [
    "upsert_provider_key",
    "get_provider_key",
    "delete_provider_key",
    "list_configured_providers",
]
