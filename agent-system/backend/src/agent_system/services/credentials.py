"""CredentialStore — Envelope-encrypted user credentials vault (v3.1 §14).

Implements dedicated credential abstraction:
save(), get(), delete(), list_metadata(), rotate(), validate(), revoke().

Secrets are encrypted at rest using envelope encryption:
  Master Encryption Key (BOB_MASTER_ENCRYPTION_KEY or derived from session/bootstrap)
        ↓ encrypts
  Data Encryption Key (DEK, per-credential AES-256 / Fernet)
        ↓ encrypts
  Credential Payload (dict / JSON)

Raw credentials are NEVER exposed to normal application logic, logs, chat history,
task descriptions, or LLM context windows. Only metadata and connection references
(e.g., `ssh:home-server` or `github:personal`) are passed to agents/tools.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any, NamedTuple

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from agent_system.config import get_settings
from agent_system.domain.ids import new_id
from agent_system.infra.db import session_scope
from agent_system.infra.models import UserCredential
from agent_system.services.secrets import redact_value

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _derive_master_key_bytes(raw_secret: str) -> bytes:
    """Derive a raw 32-byte (256-bit) master key from secret string using PBKDF2HMAC-SHA256."""
    salt = b"bob_master_credential_vault_salt_v1"
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    return kdf.derive(raw_secret.encode("utf-8"))


def get_master_key_bytes() -> bytes:
    """Resolve 256-bit master encryption key from Settings."""
    settings = get_settings()
    master_secret = (
        getattr(settings, "bob_master_encryption_key", None)
        or settings.api_session_secret
        or settings.agent_bootstrap_secret
    )
    return _derive_master_key_bytes(master_secret)


def get_master_fernet() -> Fernet:
    """Legacy helper: Resolve master Fernet encryption key from Settings."""
    raw_bytes = get_master_key_bytes()
    fernet_key = base64.urlsafe_b64encode(raw_bytes)
    return Fernet(fernet_key)


def _aes_gcm_encrypt(key_32bytes: bytes, plaintext: bytes) -> str:
    """Encrypt plaintext using AES-256-GCM, returning base64 payload of nonce + ciphertext + tag."""
    nonce = os.urandom(12)
    aesgcm = AESGCM(key_32bytes)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode("utf-8")


def _aes_gcm_decrypt(key_32bytes: bytes, b64_payload: str) -> bytes:
    """Decrypt base64-encoded nonce + ciphertext + tag using AES-256-GCM."""
    raw = base64.b64decode(b64_payload.encode("utf-8"))
    if len(raw) < 12:
        raise ValueError("Invalid AES-GCM payload: missing nonce or ciphertext")
    nonce = raw[:12]
    ciphertext = raw[12:]
    aesgcm = AESGCM(key_32bytes)
    return aesgcm.decrypt(nonce, ciphertext, None)


class CredentialMetadata(NamedTuple):
    id: str
    user_id: str
    provider: str
    name: str
    status: str
    created_at: datetime
    updated_at: datetime
    last_validated_at: datetime | None
    last_error: str | None


class CredentialStore:
    """Vault for envelope-encrypted user credentials with strict user isolation."""

    def __init__(self, session_factory: Any) -> None:
        self._factory = session_factory

    def save(
        self,
        user_id: str,
        provider: str,
        name: str,
        payload: dict[str, Any],
        *,
        status: str = "healthy",
    ) -> CredentialMetadata:
        """Encrypt and save (upsert) a user credential.

        Envelope encryption:
        1. Generate a new per-credential DEK (Data Encryption Key).
        2. Encrypt `payload` (JSON) using DEK.
        3. Encrypt DEK using Master Key.
        """
        if not user_id or not provider or not name:
            raise ValueError("user_id, provider, and name are required")

        master_key = get_master_key_bytes()
        dek = AESGCM.generate_key(bit_length=256)

        payload_bytes = json.dumps(payload).encode("utf-8")
        encrypted_blob = _aes_gcm_encrypt(dek, payload_bytes)
        encrypted_dek = _aes_gcm_encrypt(master_key, dek)

        now_ts = _now()
        with session_scope(self._factory) as db:
            row = (
                db.query(UserCredential)
                .filter(
                    UserCredential.user_id == user_id,
                    UserCredential.provider == provider,
                    UserCredential.name == name,
                )
                .one_or_none()
            )
            if row is None:
                row = UserCredential(
                    id=new_id("cred"),
                    user_id=user_id,
                    provider=provider,
                    name=name,
                    encrypted_blob=encrypted_blob,
                    encrypted_dek=encrypted_dek,
                    encryption_algorithm="AES-256-GCM-ENVELOPE",
                    status=status,
                    last_error=None,
                    created_at=now_ts,
                    updated_at=now_ts,
                    last_validated_at=now_ts if status == "healthy" else None,
                )
                db.add(row)
            else:
                row.encrypted_blob = encrypted_blob
                row.encrypted_dek = encrypted_dek
                row.status = status
                row.last_error = None
                row.updated_at = now_ts
                if status == "healthy":
                    row.last_validated_at = now_ts
            db.flush()
            meta = CredentialMetadata(
                id=row.id,
                user_id=row.user_id,
                provider=row.provider,
                name=row.name,
                status=row.status,
                created_at=row.created_at,
                updated_at=row.updated_at,
                last_validated_at=row.last_validated_at,
                last_error=row.last_error,
            )

        logger.info(
            "credential_created_or_updated",
            extra={
                "user_id": user_id,
                "credential_id": meta.id,
                "provider": provider,
                "name": name,
                "status": status,
            },
        )
        return meta

    def get(self, user_id: str, provider: str, name: str) -> dict[str, Any] | None:
        """Retrieve and decrypt raw credential payload.

        Enforces user isolation: user_id must match the owner.
        Returns None if not found or if user_id does not match.
        """
        with session_scope(self._factory) as db:
            row = (
                db.query(UserCredential)
                .filter(
                    UserCredential.user_id == user_id,
                    UserCredential.provider == provider,
                    UserCredential.name == name,
                )
                .one_or_none()
            )
            if row is None or row.status == "revoked":
                return None

            try:
                if row.encryption_algorithm == "AES-256-GCM-ENVELOPE":
                    try:
                        master_key = get_master_key_bytes()
                        dek = _aes_gcm_decrypt(master_key, row.encrypted_dek)
                        payload_bytes = _aes_gcm_decrypt(dek, row.encrypted_blob)
                        payload = json.loads(payload_bytes.decode("utf-8"))
                        if isinstance(payload, dict):
                            return payload
                    except Exception:
                        # Fallback for legacy Fernet-encrypted rows saved before AESGCM migration
                        master_fernet = get_master_fernet()
                        dek = master_fernet.decrypt(row.encrypted_dek.encode("utf-8"))
                        dek_fernet = Fernet(dek)
                        payload_bytes = dek_fernet.decrypt(row.encrypted_blob.encode("utf-8"))
                        payload = json.loads(payload_bytes.decode("utf-8"))
                        if isinstance(payload, dict):
                            return payload
                else:
                    master_fernet = get_master_fernet()
                    dek = master_fernet.decrypt(row.encrypted_dek.encode("utf-8"))
                    dek_fernet = Fernet(dek)
                    payload_bytes = dek_fernet.decrypt(row.encrypted_blob.encode("utf-8"))
                    payload = json.loads(payload_bytes.decode("utf-8"))
                    if isinstance(payload, dict):
                        return payload
                return None
            except Exception as exc:
                logger.error(
                    "credential_decryption_failed",
                    extra={
                        "user_id": user_id,
                        "provider": provider,
                        "name": name,
                        "error": str(exc),
                    },
                )
                return None

    def delete(self, user_id: str, provider: str, name: str) -> bool:
        """Hard delete a credential row. Enforces user isolation."""
        with session_scope(self._factory) as db:
            row = (
                db.query(UserCredential)
                .filter(
                    UserCredential.user_id == user_id,
                    UserCredential.provider == provider,
                    UserCredential.name == name,
                )
                .one_or_none()
            )
            if row is None:
                return False
            db.delete(row)

        logger.info(
            "credential_deleted",
            extra={"user_id": user_id, "provider": provider, "name": name},
        )
        return True

    def revoke(self, user_id: str, provider: str, name: str) -> bool:
        """Mark credential status as revoked (stops working immediately)."""
        with session_scope(self._factory) as db:
            row = (
                db.query(UserCredential)
                .filter(
                    UserCredential.user_id == user_id,
                    UserCredential.provider == provider,
                    UserCredential.name == name,
                )
                .one_or_none()
            )
            if row is None:
                return False
            row.status = "revoked"
            row.updated_at = _now()

        logger.info(
            "credential_revoked",
            extra={"user_id": user_id, "provider": provider, "name": name},
        )
        return True

    def list_metadata(self, user_id: str, provider: str | None = None) -> list[CredentialMetadata]:
        """List metadata for all credentials belonging to a specific user.

        NEVER returns decrypted secret payloads. User isolation is enforced.
        """
        with session_scope(self._factory) as db:
            query = db.query(UserCredential).filter(UserCredential.user_id == user_id)
            if provider:
                query = query.filter(UserCredential.provider == provider)
            rows = query.order_by(UserCredential.provider, UserCredential.name).all()

            return [
                CredentialMetadata(
                    id=r.id,
                    user_id=r.user_id,
                    provider=r.provider,
                    name=r.name,
                    status=r.status,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                    last_validated_at=r.last_validated_at,
                    last_error=r.last_error,
                )
                for r in rows
            ]

    def rotate(
        self, user_id: str, provider: str, name: str, new_payload: dict[str, Any]
    ) -> CredentialMetadata | None:
        """Re-encrypt and update secret payload in place while keeping metadata identity."""
        with session_scope(self._factory) as db:
            row = (
                db.query(UserCredential)
                .filter(
                    UserCredential.user_id == user_id,
                    UserCredential.provider == provider,
                    UserCredential.name == name,
                )
                .one_or_none()
            )
            if row is None:
                return None

        meta = self.save(user_id, provider, name, new_payload, status="healthy")
        logger.info(
            "credential_rotated",
            extra={"user_id": user_id, "provider": provider, "name": name},
        )
        return meta

    def update_status(
        self, user_id: str, provider: str, name: str, status: str, error: str | None = None
    ) -> bool:
        """Update health status and error for a credential."""
        with session_scope(self._factory) as db:
            row = (
                db.query(UserCredential)
                .filter(
                    UserCredential.user_id == user_id,
                    UserCredential.provider == provider,
                    UserCredential.name == name,
                )
                .one_or_none()
            )
            if row is None:
                return False
            row.status = status
            row.last_error = redact_value(error) if error else None
            row.updated_at = _now()
            if status == "healthy":
                row.last_validated_at = _now()
        return True
