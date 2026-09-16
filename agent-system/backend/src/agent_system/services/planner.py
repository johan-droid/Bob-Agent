"""Planner (v3.1 §7) — goal to explicit planned DAG.

Separated from the Supervisor by design:

**Planner** — *what work exists*

    goal -> intent -> task decomposition -> dependencies -> required
    capabilities -> expected outputs -> risk classification -> DAG

**Supervisor** (``services/orchestrator.py``) — *is that work legal and when may
it run*: validates the DAG, enforces limits, counts task creation, and schedules
ready work.

**Orchestrator** (``services/orchestrator.py``) — *how it runs*: AgentRun
lifecycle, leases/heartbeats, cancellation, recovery, completion.

The important consequence: the generic ReAct agent no longer *is* the planner.
Planning is a separate, inspectable, testable step whose output is persisted
before anything executes.

Planning strategy
-----------------
The planner is deterministic (rule-based intent + capability inference). This
is deliberate and documented rather than pretended: an LLM planner is a
plausible future extension, and until one exists the system plans the way it
actually plans. Every plan records the strategy that produced it, so no caller
can mistake a rule-based plan for a model-generated one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PLANNING_STRATEGY = "deterministic_keyword_v1"

#: Intent -> (task type, agent type, capabilities, expected outputs, risk)
INTENTS: dict[str, dict[str, Any]] = {
    "code": {
        "task_type": "code",
        "agent_type": "llm",
        "capabilities": (
            "project_detect",
            "repo_search",
            "read_source",
            "edit_source",
            "run_tests",
        ),
        "outputs": ("changed_files", "test_results", "summary"),
        "risk": "MEDIUM",
        "patterns": (
            r"\bcode\b",
            r"\brefactor\b",
            r"\bbug\b",
            r"\bfix\b",
            r"\bimplement\b",
            r"\btests?\b",
            r"\bfunction\b",
            r"\bapi\b",
            r"\bmodule\b",
        ),
    },
    "research": {
        "task_type": "research",
        "agent_type": "llm",
        "capabilities": (
            "research_search",
            "research_fetch",
            "research_extract",
            "research_citations",
        ),
        "outputs": ("findings", "citations", "summary"),
        "risk": "LOW",
        "patterns": (
            r"\bresearch\b",
            r"\binvestigate\b",
            r"\bcompare\b",
            r"\bsources?\b",
            r"\bcite\b",
            r"\bwhat is\b",
            r"\bfind out\b",
        ),
    },
    "documents": {
        "task_type": "documents",
        "agent_type": "llm",
        "capabilities": (
            "document_validate",
            "document_create",
            "document_persist",
            "document_inspect",
        ),
        "outputs": ("artifact", "summary"),
        "risk": "MEDIUM",
        "patterns": (
            r"\breport\b",
            r"\bslides?\b",
            r"\bpresentation\b",
            r"\bpptx\b",
            r"\bdocx?\b",
            r"\bxlsx\b",
            r"\bspreadsheet\b",
            r"\bpdf\b",
            r"\bdocument\b",
        ),
    },
    "browser": {
        "task_type": "browser",
        "agent_type": "llm",
        "capabilities": (
            "browser_open",
            "browser_click",
            "browser_extract",
        ),
        "outputs": ("page_content", "summary"),
        "risk": "HIGH",
        "patterns": (r"\bbrowser\b", r"\bclick\b", r"\blog ?in\b", r"\bscrape\b", r"\bform\b"),
    },
}

#: Keywords that raise the plan's risk classification.
HIGH_RISK_PATTERNS = (
    r"\bdeploy\b",
    r"\bproduction\b",
    r"\bdelete\b",
    r"\bdrop\b",
    r"\bpayment\b",
    r"\bcredential",
    r"\bsecret",
    r"\brotate\b",
    r"\bmigrate\b",
)
MAX_TASKS = 12


class PlanningError(ValueError):
    """Raised when a goal cannot be planned (never for a merely unusual goal)."""


@dataclass(frozen=True)
class PlannedTask:
    """One node of the planned DAG."""

    key: str
    title: str
    task_type: str
    agent_type: str
    input: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    risk: str = "LOW"

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "task_type": self.task_type,
            "agent_type": self.agent_type,
            "depends_on": list(self.depends_on),
            "required_capabilities": list(self.required_capabilities),
            "expected_outputs": list(self.expected_outputs),
            "risk": self.risk,
        }


@dataclass(frozen=True)
class TaskPlan:
    """The planner's output: intent, DAG, capabilities and risk."""

    goal: str
    intent: str
    tasks: tuple[PlannedTask, ...]
    risk: str
    strategy: str = PLANNING_STRATEGY
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "intent": self.intent,
            "risk": self.risk,
            "strategy": self.strategy,
            "notes": list(self.notes),
            "tasks": [task.to_json() for task in self.tasks],
        }

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        seen: list[str] = []
        for task in self.tasks:
            for capability in task.required_capabilities:
                if capability not in seen:
                    seen.append(capability)
        return tuple(seen)


class Planner:
    """Goal -> intent -> DAG (deterministic, inspectable, persisted first)."""

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings

    def plan(self, goal: str) -> TaskPlan:
        """Build the planned DAG for a goal."""
        cleaned = " ".join((goal or "").split())
        if not cleaned:
            raise PlanningError("cannot plan an empty goal")
        intent = self._classify(cleaned)
        spec: dict[str, Any] = INTENTS.get(intent, {})
        risk = self._classify_risk(cleaned, str(spec.get("risk", "LOW")))
        notes: list[str] = [
            f"planning strategy: {PLANNING_STRATEGY} (deterministic; no model call)",
        ]
        tasks = self._decompose(cleaned, intent, spec, notes)
        if len(tasks) > MAX_TASKS:
            raise PlanningError(f"plan exceeds the {MAX_TASKS}-task limit")
        return TaskPlan(
            goal=cleaned,
            intent=intent,
            tasks=tuple(tasks),
            risk=risk,
            notes=tuple(notes),
        )

    # -- internals ----------------------------------------------------------

    def _classify(self, goal: str) -> str:
        lowered = goal.lower()
        best: tuple[int, str] = (0, "generic")
        for intent, spec in INTENTS.items():
            hits = sum(1 for pattern in spec["patterns"] if re.search(pattern, lowered))
            if hits > best[0]:
                best = (hits, intent)
        return best[1]

    def _classify_risk(self, goal: str, base: str) -> str:
        lowered = goal.lower()
        if any(re.search(pattern, lowered) for pattern in HIGH_RISK_PATTERNS):
            return "HIGH"
        return base

    def _decompose(
        self, goal: str, intent: str, spec: dict[str, Any], notes: list[str]
    ) -> list[PlannedTask]:
        if intent == "generic":
            notes.append("no specialising intent matched; planning a single general task")
            return [
                PlannedTask(
                    key="task_1",
                    title=goal[:200],
                    task_type="llm",
                    agent_type="llm",
                    input={"goal": goal},
                    required_capabilities=(),
                    expected_outputs=("result", "summary"),
                    risk="LOW",
                )
            ]
        capabilities = tuple(spec["capabilities"])
        outputs = tuple(spec["outputs"])
        task_type = str(spec["task_type"])
        agent_type = str(spec["agent_type"])
        # Two-node DAG for a specialised intent: gather, then produce. The
        # dependency is explicit so the Supervisor can schedule it and the
        # Orchestrator can prove ordering.
        if intent == "documents":
            notes.append("document intents gather source material before generating output")
            return [
                PlannedTask(
                    key="gather",
                    title=f"Gather source material for: {goal[:120]}",
                    task_type="research_light",
                    agent_type=agent_type,
                    input={"goal": f"Gather the source material needed to: {goal}"},
                    required_capabilities=("research_search", "research_fetch"),
                    expected_outputs=("findings",),
                    risk="LOW",
                ),
                PlannedTask(
                    key="produce",
                    title=goal[:200],
                    task_type=task_type,
                    agent_type=agent_type,
                    input={"goal": goal},
                    depends_on=("gather",),
                    required_capabilities=capabilities,
                    expected_outputs=outputs,
                    risk="MEDIUM",
                ),
            ]
        notes.append(f"single-task plan for intent '{intent}'")
        return [
            PlannedTask(
                key="task_1",
                title=goal[:200],
                task_type=task_type,
                agent_type=agent_type,
                input={"goal": goal},
                required_capabilities=capabilities,
                expected_outputs=outputs,
                risk=str(spec["risk"]),
            )
        ]


__all__ = [
    "HIGH_RISK_PATTERNS",
    "INTENTS",
    "MAX_TASKS",
    "PLANNING_STRATEGY",
    "PlannedTask",
    "Planner",
    "PlanningError",
    "TaskPlan",
]
