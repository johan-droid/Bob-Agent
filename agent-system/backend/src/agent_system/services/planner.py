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
Two strategies, recorded on every plan so callers can tell them apart:

- ``llm_model_v1`` — the goal is decomposed via the Model Layer
  (``services/model_router.py`` + ``services/providers.py``) when a real
  provider is configured and ``planner_use_llm`` is enabled. The model call
  is recorded (``model.requested/completed`` + ``ModelCall``) like any other.
- ``deterministic_keyword_v1`` — rule-based intent + capability inference.
  Always available offline; the honest fallback when no provider is configured
  or the model call fails for any reason.
"""

from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass, field
from typing import Any

PLANNING_STRATEGY = "deterministic_keyword_v1"
PLANNING_STRATEGY_LLM = "llm_model_v1"

#: Providers that mean "no real model configured" — never attempt LLM planning.
_ECHO_PROVIDERS = {"echo", "none", ""}

PLANNER_SYSTEM_PROMPT = (
    "You are a task planner. Decompose the user goal into a small DAG of tasks. "
    "Reply with ONLY a JSON object, no prose, no fences, with keys: "
    '{"intent": str, "risk": "LOW|MEDIUM|HIGH", '
    '"tasks": [{"key": str, "title": str, "task_type": str, "agent_type": str, '
    '"goal": str, "depends_on": [str], '
    '"required_capabilities": [str], "expected_outputs": [str], "risk": str}]}. '
    "Keep 1-4 tasks. depends_on may only reference keys in the same reply. "
    "required_capabilities must come from the allowed list when one is given."
)

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
    """Goal -> intent -> DAG (LLM via Model Layer first, deterministic fallback)."""

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings

    def plan(
        self,
        goal: str,
        factory: Any = None,
        bus: Any = None,
        model_router: Any = None,
        session_id: str | None = None,
    ) -> TaskPlan:
        """Build the planned DAG for a goal.

        When ``planner_use_llm`` is enabled and a real provider is configured,
        the Model Layer is consulted first (strategy ``llm_model_v1``). Any
        failure — no provider, no factory for cost accounting, bad JSON,
        invalid DAG — falls back to the deterministic planner, with the reason
        recorded in ``notes`` so the fallback is never silent.
        """
        cleaned = " ".join((goal or "").split())
        if not cleaned:
            raise PlanningError("cannot plan an empty goal")
        llm_plan = self._try_llm_plan(cleaned, factory, bus, model_router, session_id)
        if llm_plan is not None:
            return llm_plan
        return self._deterministic_plan(cleaned)

    def _deterministic_plan(self, cleaned: str) -> TaskPlan:
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

    def _llm_enabled(self) -> bool:
        if self._settings is None:
            return False
        if not bool(getattr(self._settings, "planner_use_llm", True)):
            return False
        provider = str(getattr(self._settings, "default_provider", "echo") or "echo")
        return provider.strip().lower() not in _ECHO_PROVIDERS

    def _try_llm_plan(
        self,
        cleaned: str,
        factory: Any,
        bus: Any,
        model_router: Any,
        session_id: str | None,
    ) -> TaskPlan | None:
        """Attempt Model Layer planning; None means fall back (never raises)."""
        if not self._llm_enabled():
            return None
        try:
            router = model_router or self._build_router(bus)
            if router is None:
                return None
            model_id = self._resolve_model_id()
            prompt = self._llm_prompt(cleaned)
            if factory is None:
                return None  # cost accounting needs a factory; stay honest
            result = router.invoke(
                factory,
                model_id,
                prompt,
                session_id=session_id,
                agent_type="planner",
            )
            if not result.ok or not result.output:
                return None
            return self._parse_llm_plan(cleaned, result.output, result.model_id)
        except Exception:
            return None  # any model/plumbing failure -> deterministic fallback

    def _build_router(self, bus: Any) -> Any | None:
        try:
            from agent_system.services.providers import build_model_router, configured_providers
        except Exception:
            return None
        try:
            settings = self._settings
            event_bus = bus
            if event_bus is None:
                from agent_system.infra.event_bus import EventBus

                event_bus = EventBus()
            configured = [p["key"] for p in configured_providers(settings) if p["configured"]]
            if not configured:
                return None
            return build_model_router(event_bus, settings, provider_names=configured)
        except Exception:
            return None

    def _resolve_model_id(self) -> str:
        override = str(getattr(self._settings, "planner_model", "") or "").strip()
        if override:
            return override
        try:
            from agent_system.services.providers import default_model_id

            return str(default_model_id(self._settings))
        except Exception:
            return "echo-default"

    def _llm_prompt(self, goal: str) -> str:
        allowed = ""
        try:
            from agent_system.services.tools.registry import build_registry

            names = build_registry(self._settings).names()
            allowed = f" Allowed required_capabilities: {', '.join(names[:80])}."
        except Exception:
            pass
        return (
            f"{PLANNER_SYSTEM_PROMPT}{allowed}\n\nGoal:\n{goal[:2000]}\n\nJSON:"
        )

    def _parse_llm_plan(self, goal: str, output: str, model_id: str) -> TaskPlan | None:
        try:
            data = _extract_json(output)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks or len(raw_tasks) > MAX_TASKS:
            return None
        known_caps: set[str] | None = None
        try:
            from agent_system.services.tools.registry import build_registry

            known_caps = set(build_registry(self._settings).names())
        except Exception:
            known_caps = None
        tasks: list[PlannedTask] = []
        keys: set[str] = set()
        for i, raw in enumerate(raw_tasks):
            if not isinstance(raw, dict):
                return None
            key = re.sub(r"[^a-zA-Z0-9_]+", "_", str(raw.get("key") or f"task_{i + 1}"))[:40]
            if not key or key in keys:
                return None
            keys.add(key)
            caps = tuple(
                str(c) for c in (raw.get("required_capabilities") or []) if str(c).strip()
            )
            if known_caps is not None:
                caps = tuple(c for c in caps if c in known_caps)
            deps = tuple(str(d) for d in (raw.get("depends_on") or []) if str(d).strip())
            if any(d not in keys and d != key for d in deps if d not in [t.key for t in tasks] + [key]):
                # Forward/unknown refs are rejected; keep only already-seen keys.
                deps = tuple(d for d in deps if d in keys)
            risk = str(raw.get("risk") or "LOW").upper()
            if risk not in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
                risk = "LOW"
            task_goal = str(raw.get("goal") or raw.get("title") or goal)[:2000]
            tasks.append(
                PlannedTask(
                    key=key,
                    title=str(raw.get("title") or task_goal[:200])[:200],
                    task_type=str(raw.get("task_type") or "llm")[:40],
                    agent_type=str(raw.get("agent_type") or "llm")[:40],
                    input={"goal": task_goal},
                    depends_on=deps,
                    required_capabilities=caps,
                    expected_outputs=tuple(
                        str(o) for o in (raw.get("expected_outputs") or ["result"])[:8]
                    ),
                    risk=risk,
                )
            )
        # Cycle check on keys (supervisor re-validates before persist).
        graph = {t.key: list(t.depends_on) for t in tasks}
        if _has_cycle(graph):
            return None
        intent = str(data.get("intent") or "generic")[:40]
        risk = str(data.get("risk") or tasks[0].risk).upper()
        if risk not in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
            risk = self._classify_risk(goal, "LOW")
        else:
            risk = self._classify_risk(goal, risk)
        return TaskPlan(
            goal=goal,
            intent=intent,
            tasks=tuple(tasks),
            risk=risk,
            strategy=PLANNING_STRATEGY_LLM,
            notes=(f"planning strategy: {PLANNING_STRATEGY_LLM} via {model_id}",),
        )

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
    "PLANNER_SYSTEM_PROMPT",
    "PLANNING_STRATEGY",
    "PLANNING_STRATEGY_LLM",
    "PlannedTask",
    "Planner",
    "PlanningError",
    "TaskPlan",
]


def _extract_json(output: str) -> Any:
    """Extract the first JSON object from model output (fences tolerated)."""
    text = output.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return _json.loads(fenced.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return _json.loads(text[start : end + 1])


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in done:
            return False
        visiting.add(node)
        for dep in graph.get(node, []):
            if dep in graph and visit(dep):
                return True
        visiting.discard(node)
        done.add(node)
        return False

    return any(visit(n) for n in graph)
