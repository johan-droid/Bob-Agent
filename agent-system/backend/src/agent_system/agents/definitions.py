"""Explicit agent definitions and routing (v3.1 §3, §4).

An agent is a *policy*, not a code path. Every agent declares what it is for,
what it may use, and how it is constrained:

    name · version · description · supported task types · required capabilities
    allowed skills · model policy · resource limits · permission policy ·
    execution mode

Several agents may share one execution engine (the ReAct loop). The distinction
comes from capabilities, skills, routing and constraints — not from duplicated
code, which is why there is exactly one LLM execution path behind all of them.

Routing is explicit (v3.1 §4 "do not silently convert arbitrary unknown agent
types into generic LLM execution"):

- a registered type resolves to its own definition, no fallback;
- an unregistered type resolves to ``generic`` **only** when an explicit
  fallback handler is installed, and the resolution carries
  ``fallback_reason="unsupported_agent_type"``;
- the fallback decision is recorded as an ``agent.fallback_applied`` event, so
  it is visible in the trace rather than being invisible routing behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Permission policies an agent may declare.
PERMISSION_ASK = "ask"
PERMISSION_READ_ONLY = "read_only"
PERMISSION_DEFAULT_DENY = "default_deny"

#: Execution modes.
MODE_REACT = "react"  # LLM reason+act over the capability library
MODE_DETERMINISTIC = "deterministic"  # no model, honest builtin
MODE_EXTERNAL = "external"  # handler owned by an external integration


@dataclass(frozen=True)
class AgentDefinition:
    """What one agent is, may use, and is constrained by."""

    name: str
    version: str = "1.0.0"
    description: str = ""
    task_types: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    allowed_skills: tuple[str, ...] = ()
    model_policy: str = "default"
    resource_limits: dict[str, Any] = field(default_factory=dict)
    permission_policy: str = PERMISSION_ASK
    execution_mode: str = MODE_REACT

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "task_types": list(self.task_types),
            "required_capabilities": list(self.required_capabilities),
            "allowed_skills": list(self.allowed_skills),
            "model_policy": self.model_policy,
            "resource_limits": dict(self.resource_limits),
            "permission_policy": self.permission_policy,
            "execution_mode": self.execution_mode,
        }


@dataclass(frozen=True)
class AgentResolution:
    """Which definition will handle a task, and why."""

    requested_type: str
    definition: AgentDefinition
    handler: Any
    fallback_reason: str | None = None

    @property
    def is_fallback(self) -> bool:
        return self.fallback_reason is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "requested_type": self.requested_type,
            "resolved_agent": self.definition.name,
            "execution_mode": self.execution_mode,
            "fallback_reason": self.fallback_reason,
        }

    @property
    def execution_mode(self) -> str:
        return self.definition.execution_mode


#: First-party agent definitions (v3.1 §3). Kept deliberately small: these are
#: policies over one execution engine, not seven copies of it.
GENERIC = AgentDefinition(
    name="generic",
    description="General-purpose agent: any goal, tools as needed, no specialisation.",
    task_types=("*",),
    permission_policy=PERMISSION_ASK,
    execution_mode=MODE_REACT,
)
CODE = AgentDefinition(
    name="code",
    description="Reads, edits and verifies source code in a workspace.",
    task_types=("code", "coding", "refactor", "bugfix", "feature"),
    required_capabilities=(
        "project_detect",
        "repo_search",
        "read_source",
        "edit_source",
        "run_tests",
        "run_linter",
        "run_typecheck",
    ),
    allowed_skills=("tester",),
    permission_policy=PERMISSION_ASK,
    execution_mode=MODE_REACT,
)
RESEARCH = AgentDefinition(
    name="research",
    description="Finds sources, extracts findings and preserves provenance.",
    task_types=("research", "investigate", "summarise"),
    required_capabilities=(
        "research_search",
        "research_fetch",
        "research_extract",
        "research_citations",
    ),
    allowed_skills=("research", "web-research"),
    permission_policy=PERMISSION_ASK,
    execution_mode=MODE_REACT,
)
BROWSER = AgentDefinition(
    name="browser",
    description="Drives an interactive browser session; payment flows are refused.",
    task_types=("browser", "scrape", "automate"),
    required_capabilities=(
        "browser_open",
        "browser_click",
        "browser_type",
        "browser_extract",
    ),
    permission_policy=PERMISSION_ASK,
    execution_mode=MODE_EXTERNAL,
)
DOCUMENT = AgentDefinition(
    name="documents",
    description="Builds and inspects persisted documents (pptx/docx/xlsx/pdf).",
    task_types=("document", "documents", "report"),
    required_capabilities=(
        "document_validate",
        "document_create",
        "document_persist",
        "document_inspect",
    ),
    allowed_skills=("document-craft",),
    permission_policy=PERMISSION_ASK,
    execution_mode=MODE_EXTERNAL,
)
QA = AgentDefinition(
    name="qa",
    description="Generates and runs tests, reporting a structured QA result.",
    task_types=("qa", "test", "verify"),
    required_capabilities=("run_tests", "read_source"),
    allowed_skills=("qa-assist",),
    permission_policy=PERMISSION_ASK,
    execution_mode=MODE_EXTERNAL,
)
SCHEDULER = AgentDefinition(
    name="scheduler",
    description="Fires scheduled goals as sessions; performs no work of its own.",
    task_types=("schedule", "cron"),
    required_capabilities=(),
    permission_policy=PERMISSION_DEFAULT_DENY,
    execution_mode=MODE_DETERMINISTIC,
)
LLM = AgentDefinition(
    name="llm",
    description="ReAct execution engine over the capability library (the worker's default).",
    task_types=("llm",),
    permission_policy=PERMISSION_ASK,
    execution_mode=MODE_REACT,
)
DETERMINISTIC = AgentDefinition(
    name="builtin",
    description="Deterministic builtin handler: performs no model call and fabricates nothing.",
    task_types=("builtin",),
    permission_policy=PERMISSION_READ_ONLY,
    execution_mode=MODE_DETERMINISTIC,
)

#: The declared catalog, keyed by canonical agent name.
BUILTIN_DEFINITIONS: dict[str, AgentDefinition] = {
    definition.name: definition
    for definition in (
        GENERIC,
        CODE,
        RESEARCH,
        BROWSER,
        DOCUMENT,
        QA,
        SCHEDULER,
        LLM,
        DETERMINISTIC,
    )
}

#: Aliases so existing task types route to the closest declared agent.
ALIASES: dict[str, str] = {
    "research_agent": "research",
    "browser_research": "research",
    "document": "documents",
    "document_agent": "documents",
    "qa_agent": "qa",
    "code_agent": "code",
    "coder": "code",
    "generic_agent": "generic",
    "scheduler_agent": "scheduler",
}


def definition_for(agent_type: str) -> AgentDefinition | None:
    """Look up a declared definition by name or alias (None when unknown)."""
    key = (agent_type or "").strip().lower()
    if not key:
        return None
    if key in BUILTIN_DEFINITIONS:
        return BUILTIN_DEFINITIONS[key]
    alias = ALIASES.get(key)
    return BUILTIN_DEFINITIONS.get(alias) if alias else None


def ad_hoc_definition(agent_type: str) -> AgentDefinition:
    """Definition for a handler registered by an integration at composition time."""
    return AgentDefinition(
        name=agent_type,
        description=f"Dynamically registered agent type '{agent_type}'.",
        task_types=(agent_type,),
        execution_mode=MODE_EXTERNAL,
    )


__all__ = [
    "ALIASES",
    "BROWSER",
    "BUILTIN_DEFINITIONS",
    "CODE",
    "DETERMINISTIC",
    "DOCUMENT",
    "GENERIC",
    "LLM",
    "MODE_DETERMINISTIC",
    "MODE_EXTERNAL",
    "MODE_REACT",
    "PERMISSION_ASK",
    "PERMISSION_DEFAULT_DENY",
    "PERMISSION_READ_ONLY",
    "QA",
    "RESEARCH",
    "SCHEDULER",
    "AgentDefinition",
    "AgentResolution",
    "ad_hoc_definition",
    "definition_for",
]
