"""Worker specialization registry (Agentic Runtime v1, additive)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkerRole:
    role: str
    description: str = ""
    requires_tool_calling: bool = True
    min_context: int = 0
    min_coding: int = 0
    requires_vision: bool = False
    prefers_latency: str = ""
    prefers_cost: str = ""
    fallback_order: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    preferred_context: str = ""
    max_workers_default: int = 4

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "description": self.description,
            "requires_tool_calling": self.requires_tool_calling,
            "min_context": self.min_context,
            "min_coding": self.min_coding,
            "requires_vision": self.requires_vision,
            "prefers_latency": self.prefers_latency,
            "prefers_cost": self.prefers_cost,
            "fallback_order": list(self.fallback_order),
            "required_capabilities": list(self.required_capabilities),
            "preferred_context": self.preferred_context,
        }


WORKER_ROLES: dict[str, WorkerRole] = {
    "RESEARCHER": WorkerRole(
        role="RESEARCHER",
        description="Web research with citations.",
        requires_tool_calling=True,
        min_context=32000,
        fallback_order=("gemini", "groq", "openrouter", "ollama"),
        required_capabilities=("research_search", "research_fetch"),
    ),
    "CODER": WorkerRole(
        role="CODER",
        description="Filesystem + shell coding with tool calling.",
        requires_tool_calling=True,
        min_context=32000,
        min_coding=2,
        fallback_order=("nim", "groq", "gemini", "openrouter"),
        required_capabilities=("read_source", "edit_source"),
        preferred_context="large",
    ),
    "DEBUGGER": WorkerRole(
        role="DEBUGGER",
        description="Fast diagnosis loops.",
        requires_tool_calling=True,
        prefers_latency="fast",
        fallback_order=("groq", "nim", "gemini"),
        required_capabilities=("read_source", "run_tests"),
    ),
    "SECURITY_AUDITOR": WorkerRole(
        role="SECURITY_AUDITOR",
        description="Security review and hardening.",
        requires_tool_calling=True,
        min_context=32000,
        min_coding=2,
        fallback_order=("nim", "gemini", "groq"),
        required_capabilities=("read_source", "repo_search"),
    ),
    "BROWSER_AGENT": WorkerRole(
        role="BROWSER_AGENT",
        description="Browser automation and extraction.",
        requires_tool_calling=True,
        requires_vision=True,
        fallback_order=("gemini", "openrouter"),
        required_capabilities=("browser_open", "browser_extract"),
    ),
    "DATABASE_ENGINEER": WorkerRole(
        role="DATABASE_ENGINEER",
        description="Schema and query work.",
        requires_tool_calling=True,
        min_coding=2,
        fallback_order=("nim", "groq", "gemini"),
        required_capabilities=("read_source", "edit_source"),
    ),
    "FRONTEND_ENGINEER": WorkerRole(
        role="FRONTEND_ENGINEER",
        description="UI implementation.",
        requires_tool_calling=True,
        min_coding=2,
        fallback_order=("nim", "groq", "openrouter"),
        required_capabilities=("read_source", "edit_source"),
    ),
    "BACKEND_ENGINEER": WorkerRole(
        role="BACKEND_ENGINEER",
        description="API and service implementation.",
        requires_tool_calling=True,
        min_coding=2,
        fallback_order=("nim", "groq", "gemini"),
        required_capabilities=("read_source", "edit_source", "run_tests"),
    ),
    "TEST_ENGINEER": WorkerRole(
        role="TEST_ENGINEER",
        description="Test generation and execution.",
        requires_tool_calling=True,
        prefers_latency="fast",
        fallback_order=("groq", "nim", "gemini"),
        required_capabilities=("run_tests", "read_source"),
    ),
    "DEVOPS_AGENT": WorkerRole(
        role="DEVOPS_AGENT",
        description="Build, packaging and deploy checks.",
        requires_tool_calling=True,
        fallback_order=("groq", "nim", "openrouter"),
        required_capabilities=("read_source", "run_tests"),
    ),
    "DOCUMENTATION_AGENT": WorkerRole(
        role="DOCUMENTATION_AGENT",
        description="Docs and release notes.",
        requires_tool_calling=False,
        fallback_order=("gemini", "groq", "openrouter"),
        required_capabilities=("document_create",),
    ),
    "REVIEWER": WorkerRole(
        role="REVIEWER",
        description="Master verification of worker outputs.",
        requires_tool_calling=False,
        min_context=32000,
        fallback_order=("gemini", "nim", "groq"),
        required_capabilities=(),
    ),
}


def role_for(name: str) -> WorkerRole | None:
    if not name:
        return None
    return WORKER_ROLES.get(name.strip().upper())


def list_roles() -> list[WorkerRole]:
    return list(WORKER_ROLES.values())


__all__ = ["WORKER_ROLES", "WorkerRole", "list_roles", "role_for"]
