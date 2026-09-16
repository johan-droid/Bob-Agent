"""API v1 settings endpoints — read/write configuration from the web dashboard.

Mirrors the agentctl settings CLI: list/get/set grouped settings backed by
.env.local (gitignored, overrides .env). Secrets are masked in responses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agent_system.api.deps import get_authenticator

settings_router = APIRouter(prefix="/api/v1", dependencies=[Depends(get_authenticator)])


class SettingOut(BaseModel):
    key: str
    value: str
    source: str
    group: str
    help: str
    secret: bool


class SettingsGroupOut(BaseModel):
    group: str
    settings: list[SettingOut]


class SettingUpdate(BaseModel):
    value: str = Field(min_length=0, max_length=10_000)


class SettingUpdateOut(BaseModel):
    key: str
    value: str
    source: str


def _settings_service() -> tuple[
    dict[str, str],
    dict[str, str],
    Callable[..., dict[str, Any]],
    Callable[..., list[dict[str, Any]]],
    Callable[..., dict[str, Any]],
]:
    """Lazy import to avoid circular deps at module load."""
    from agent_system.cli.settings import (
        _GROUPS,
        _HELP,
        get_setting,
        list_settings,
        set_setting,
    )

    return _GROUPS, _HELP, get_setting, list_settings, set_setting


@settings_router.get("/settings")
def list_all_settings(request: Request, show_secrets: bool = False) -> list[SettingsGroupOut]:
    """List every setting, grouped by area. Secrets masked unless show_secrets."""
    _GROUPS, _HELP, _, list_settings, _ = _settings_service()
    rows = list_settings(show_secrets=show_secrets)
    grouped: dict[str, list[SettingOut]] = {}
    for row in rows:
        group = row.get("group", "other")
        if group not in grouped:
            grouped[group] = []
        grouped[group].append(SettingOut(**row))
    return [SettingsGroupOut(group=g, settings=items) for g, items in grouped.items()]


@settings_router.get("/settings/{key}")
def get_one_setting(request: Request, key: str, show_secrets: bool = False) -> SettingOut:
    """Get a single setting by key."""
    _, _, get_setting, _, _ = _settings_service()
    try:
        row = get_setting(key, show_secrets=show_secrets)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SettingOut(**row)


@settings_router.post("/settings/{key}")
def update_setting(key: str, body: SettingUpdate, request: Request) -> SettingUpdateOut:
    """Validate and persist a setting to .env.local."""
    _, _, _, _, set_setting = _settings_service()
    try:
        row = set_setting(key, body.value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SettingUpdateOut(**row)


@settings_router.get("/settings-groups")
def settings_groups(request: Request) -> dict[str, dict[str, str]]:
    """Return the group mapping for all settings keys."""
    _GROUPS, _HELP, *_ = _settings_service()
    return {"groups": _GROUPS, "help": _HELP}
