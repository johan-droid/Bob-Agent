"""Shared FastAPI dependency providers.

Lives outside main.py to avoid an import cycle: main.py imports the v1
routers, which import these providers.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from agent_system.services.auth import Authenticator, require_auth
from agent_system.services.permissions import PermissionGate


def get_session_factory(request: Request) -> Any:
    return request.app.state.session_factory


def get_gate(request: Request) -> PermissionGate:
    gate: PermissionGate = request.app.state.gate
    return gate


def get_authenticator(request: Request) -> Authenticator:
    authenticator: Authenticator = request.app.state.authenticator
    require_auth(request, authenticator)
    return authenticator
