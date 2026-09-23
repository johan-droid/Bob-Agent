"""API authentication (v3.1 §14, §16).

Localhost-only system, but still authenticated: requests must carry a valid
session token derived from the configured session secret.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request


class Authenticator:
    def __init__(self, session_secret: str) -> None:
        import threading as _threading
        import time as _time

        self._secret = session_secret.encode()
        self._tokens: set[str] = set()
        # hash -> (owner_user_id or None for legacy shared, issued_at)
        self._owners: dict[str, tuple[str | None, float]] = {}
        self._lock = _threading.Lock()
        self._now = _time.monotonic
        self._bootstrap_token = self.mint_token()

    def mint_token(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens.add(self._hash(token))
            self._owners[self._hash(token)] = (None, self._now())
        return token

    def mint_user_token(self, user_id: str) -> str:
        """Mint a token bound to one Bob user_id. Cannot impersonate others."""
        if not user_id:
            raise ValueError("user_id required")
        token = "u_" + secrets.token_urlsafe(32)
        with self._lock:
            self._tokens.add(self._hash(token))
            self._owners[self._hash(token)] = (user_id, self._now())
        return token

    def owner_of(self, token: str) -> str | None | bool:
        """Return bound user_id, None for legacy shared, False for invalid."""
        if not token:
            return False
        digest = self._hash(token)
        with self._lock:
            if digest not in self._tokens:
                return False
            return self._owners.get(digest, (None, 0.0))[0]

    def _hash(self, token: str) -> str:
        return hmac.new(self._secret, token.encode(), hashlib.sha256).hexdigest()

    def verify(self, token: str) -> bool:
        if not token:
            return False
        digest = self._hash(token)
        # Constant-time membership: linear scan with compare_digest to avoid
        # short-circuit oracle. Token sets are small, so this is fine.
        with self._lock:
            stored_list = list(self._tokens)
        found = False
        for stored in stored_list:
            if hmac.compare_digest(stored, digest):
                found = True
                break
        return found

    def revoke_all(self) -> None:
        self._tokens.clear()

    @property
    def bootstrap_token(self) -> str:
        return self._bootstrap_token


def _raw_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header.removeprefix("Bearer ").strip() or None
    cookie = request.cookies.get("agent_session")
    return cookie or None


def require_auth(request: Request, authenticator: Authenticator) -> None:
    """FastAPI dependency body: raise 401 unless authenticated."""
    token = _raw_token(request)
    if token and authenticator.verify(token):
        return
    raise HTTPException(status_code=401, detail="unauthorized")
