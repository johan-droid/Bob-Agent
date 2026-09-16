"""Agent skills API — Hermes-style pluggable capabilities.

- GET    /api/v1/skills             : list skills (summaries)
- GET    /api/v1/skills/{name}      : full skill (instructions + config)
- POST   /api/v1/skills             : create a skill (users AND agents use this)
- PATCH  /api/v1/skills/{name}      : enable/disable + config values
- DELETE /api/v1/skills/{name}      : remove a skill
- POST   /api/v1/skills/import      : import from a local path / .md URL / git URL

State changes emit ``skill.created / skill.updated / skill.deleted`` events.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agent_system.api.deps import get_authenticator
from agent_system.config import get_settings
from agent_system.domain.events import Event
from agent_system.infra.db import session_scope
from agent_system.services.skills import (
    SkillError,
    SkillExistsError,
    SkillManager,
    SkillNotFoundError,
    SkillValidationError,
)

skills_router = APIRouter(prefix="/api/v1/skills", dependencies=[Depends(get_authenticator)])


def _manager(request: Request | None = None) -> SkillManager:
    settings = get_settings()
    return SkillManager(settings.skills_dir)


def _emit(request: Request, event_type: str, payload: dict[str, Any]) -> None:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        bus = request.app.state.event_bus
        bus.emit(Event(type=event_type, actor="skills", payload=payload), db)


def _http(exc: SkillError) -> HTTPException:
    if isinstance(exc, SkillNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (SkillExistsError, SkillValidationError)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@skills_router.get("")
def list_skills(request: Request, agent_type: str | None = None) -> dict[str, Any]:
    """List skill summaries, optionally filtered to an agent type."""
    mgr = _manager(request)
    skills = mgr.enabled(agent_type) if agent_type else mgr.discover()
    return {"skills": [s.summary() for s in skills], "errors": mgr.last_errors}


@skills_router.get("/{name}")
def get_skill(name: str, request: Request) -> dict[str, Any]:
    """Full skill detail including instructions + effective config."""
    try:
        return _manager(request).get(name).detail()
    except SkillError as exc:
        raise _http(exc) from exc


class SkillCreate(BaseModel):
    name: str = Field(min_length=2, max_length=41, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=20000)
    agents: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    author: str = Field(default="user", max_length=80)


@skills_router.post("", status_code=201)
def create_skill(body: SkillCreate, request: Request) -> dict[str, Any]:
    """Create a skill — called by users, the CLI, and agents themselves."""
    try:
        skill = _manager(request).create_skill(
            body.name,
            body.description,
            body.instructions,
            agents=body.agents,
            config=body.config,
            author=body.author,
        )
    except SkillError as exc:
        raise _http(exc) from exc
    _emit(request, "skill.created", {"name": skill.name, "author": skill.author})
    return skill.detail()


class SkillPatch(BaseModel):
    enabled: bool | None = None
    config: dict[str, Any] | None = None


@skills_router.patch("/{name}")
def update_skill(name: str, body: SkillPatch, request: Request) -> dict[str, Any]:
    """Enable/disable a skill and/or merge config values."""
    mgr = _manager(request)
    try:
        skill = mgr.get(name)
        if body.enabled is not None:
            skill = mgr.set_enabled(name, body.enabled)
        if body.config:
            for key, value in body.config.items():
                skill = mgr.set_config(name, key, value)
    except SkillError as exc:
        raise _http(exc) from exc
    _emit(
        request,
        "skill.updated",
        {"name": name, "enabled": skill.enabled, "config_keys": sorted(skill.config)},
    )
    return skill.detail()


@skills_router.delete("/{name}")
def delete_skill(name: str, request: Request) -> dict[str, str]:
    """Remove a skill entirely."""
    try:
        _manager(request).delete_skill(name)
    except SkillError as exc:
        raise _http(exc) from exc
    _emit(request, "skill.deleted", {"name": name})
    return {"deleted": name}


class SkillImport(BaseModel):
    source: str = Field(min_length=1, max_length=2000)
    overwrite: bool = False


@skills_router.post("/import", status_code=201)
def import_skills(body: SkillImport, request: Request) -> dict[str, Any]:
    """Import skill(s) from a local path, an .md URL, or a git URL."""
    try:
        added = _manager(request).add_skill(body.source, overwrite=body.overwrite)
    except SkillError as exc:
        raise _http(exc) from exc
    _emit(request, "skill.created", {"names": [s.name for s in added], "source": body.source})
    return {"skills": [s.summary() for s in added]}
