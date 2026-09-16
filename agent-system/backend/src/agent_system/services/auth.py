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
        self._secret = session_secret.encode()
        self._tokens: set[str] = set()
        self._bootstrap_token = self.mint_token()

    def mint_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens.add(self._hash(token))
        return token

    def _hash(self, token: str) -> str:
        return hmac.new(self._secret, token.encode(), hashlib.sha256).hexdigest()

    def verify(self, token: str) -> bool:
        return self._hash(token) in self._tokens

    def revoke_all(self) -> None:
        self._tokens.clear()

    @property
    def bootstrap_token(self) -> str:
        return self._bootstrap_token


def require_auth(request: Request, authenticator: Authenticator) -> None:
    """FastAPI dependency body: raise 401 unless authenticated."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        token = header.removeprefix("Bearer ").strip()
        if authenticator.verify(token):
            return
    # Browser-friendly fallback for localhost dashboard: signed cookie.
    cookie = request.cookies.get("agent_session")
    if cookie and authenticator.verify(cookie):
        return
    raise HTTPException(status_code=401, detail="unauthorized")
