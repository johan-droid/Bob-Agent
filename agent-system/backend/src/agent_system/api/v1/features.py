"""API v1 additions for Phases 15–17: recordings/replay, batches, recipes,
personalities, insights.

Same contract style as router.py: versioned paths, Pydantic schemas, auth,
event emission on state changes.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from agent_system.api.deps import (
    apply_owner_filter,
    enforce_owner_row,
    enforce_session_visible,
    enforce_task_visible,
    get_authenticator,
    get_principal,
    owner_id,
)
from agent_system.config import get_settings
from agent_system.domain import ids
from agent_system.domain.events import Event, utcnow
from agent_system.infra.db import session_scope
from agent_system.infra.event_bus import EventBus
from agent_system.infra.models import Insight, ScheduledJob, Task
from agent_system.services.permissions import Decision
from agent_system.services.recording import (
    ReplayBlockedError,
    ReplayContext,
    ReplayError,
    ReplayService,
    recordings_summary,
)

authenticated_features = APIRouter(prefix="/api/v1", dependencies=[Depends(get_authenticator)])


def _bus(request: Request) -> EventBus:
    bus: EventBus = request.app.state.event_bus
    return bus


def _emit(
    request: Request, event_type: str, actor: str, payload: dict[str, Any], **links: Any
) -> None:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        _bus(request).emit(Event(type=event_type, actor=actor, payload=payload, **links), db)


# ---------------------------------------------------------------------------
# Phase 15 — Recordings + Replay
# ---------------------------------------------------------------------------


@authenticated_features.get("/recordings")
def list_recordings(request: Request) -> list[dict[str, Any]]:
    settings = get_settings()
    factory = request.app.state.session_factory
    return recordings_summary(factory, settings.recordings_dir)


class ReplayRequest(BaseModel):
    mode: str = Field(pattern=r"^(INSPECT|SIMULATE|APPROVED_REEXECUTE)$")
    context: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = None


@authenticated_features.post("/recordings/{recording_id}/replay")
def replay_recording(recording_id: str, body: ReplayRequest, request: Request) -> dict[str, Any]:
    settings = get_settings()
    service = ReplayService(settings.recordings_dir)
    current = ReplayContext.from_dict(body.context)
    try:
        result = service.replay(recording_id, body.mode, current, approval_id=body.approval_id)
    except ReplayBlockedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ReplayError as exc:
        if "not found" in str(exc):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _emit(
        request,
        "tool.completed",
        "replay",
        {"recording_id": recording_id, "mode": body.mode, "allowed": result.allowed},
    )
    return {
        "recording_id": result.recording_id,
        "mode": result.mode,
        "allowed": result.allowed,
        "blocked_reason": result.blocked_reason,
        "fingerprint_match": result.fingerprint_match,
        "fingerprint_diff": {k: list(v) for k, v in result.fingerprint_diff.items()},
        "steps": result.steps,
        "side_effects": result.side_effects,
    }


# ---------------------------------------------------------------------------
# Phase 16 — Task batching
# ---------------------------------------------------------------------------


class BatchCreate(BaseModel):
    session_id: str
    task_ids: list[str] = Field(min_length=2, max_length=100)
    batch_type: str = Field(default="parallel", pattern=r"^[a-z_]{1,40}$")


@authenticated_features.post("/batches", status_code=201)
def create_batch(
    body: BatchCreate,
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
) -> dict[str, Any]:
    from agent_system.services.batching import BatchError, TaskBatcher

    factory = request.app.state.session_factory
    # Owner check: all batched tasks + session must belong to the caller.
    with session_scope(factory) as db:
        enforce_session_visible(db, body.session_id, principal)
        for tid in body.task_ids:
            enforce_task_visible(db, tid, principal)
    batcher = TaskBatcher(_bus(request))
    try:
        result = batcher.create_batch(factory, body.session_id, body.task_ids, body.batch_type)
    except BatchError as exc:
        status = 409 if "compatib" in str(exc).lower() else 404
        raise HTTPException(status_code=status, detail="batch error") from exc
    # Stamp owner on the batch row for list/get/cancel isolation.
    try:
        from agent_system.infra.models import TaskBatch

        with session_scope(factory) as db:
            row = db.get(TaskBatch, str(result.get("batch_id")))
            if row is not None and getattr(row, "owner_user_id", None) is None:
                row.owner_user_id = owner_id(principal)
    except Exception:
        pass
    _emit(request, "task.queued", "batcher", {"batch_id": result["batch_id"]})
    return result


@authenticated_features.post("/batches/{batch_id}/cancel")
def cancel_batch(
    batch_id: str,
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
) -> dict[str, Any]:
    from agent_system.services.batching import BatchError, TaskBatcher

    factory = request.app.state.session_factory
    try:
        from agent_system.infra.models import TaskBatch

        with session_scope(factory) as db:
            enforce_owner_row(db.get(TaskBatch, batch_id), principal, "batch")
    except HTTPException:
        raise
    except Exception:
        pass
    batcher = TaskBatcher(_bus(request))
    try:
        result = batcher.cancel_batch(factory, batch_id)
    except BatchError as exc:
        raise HTTPException(status_code=404, detail="batch not found") from exc
    return result


@authenticated_features.get("/batches/{batch_id}")
def get_batch(
    batch_id: str,
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
) -> dict[str, Any]:
    from agent_system.services.batching import batch_status

    factory = request.app.state.session_factory
    try:
        from agent_system.infra.models import TaskBatch

        with session_scope(factory) as db:
            enforce_owner_row(db.get(TaskBatch, batch_id), principal, "batch")
    except HTTPException:
        raise
    except Exception:
        pass
    try:
        return batch_status(factory, batch_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="batch not found") from exc


# ---------------------------------------------------------------------------
# Phase 16 — Recipes
# ---------------------------------------------------------------------------


class RecipeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    task_dag: dict[str, Any]  # {"tasks": [{key, task_type, title, depends_on, agent_type, input}]}
    parameters: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


@authenticated_features.post("/recipes", status_code=201)
def create_recipe(
    body: RecipeCreate,
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
) -> dict[str, Any]:
    from agent_system.services.recipes import RecipeEngine, RecipeError

    if len(str(body.task_dag)) > 100 * 1024:
        raise HTTPException(status_code=413, detail="task_dag too large")
    factory = request.app.state.session_factory
    engine = RecipeEngine(_bus(request))
    try:
        recipe = engine.create_recipe(
            factory,
            body.name,
            body.task_dag,
            body.parameters,
            description=body.description,
            tags=body.tags,
        )
    except RecipeError as exc:
        raise HTTPException(status_code=422, detail="invalid recipe") from exc
    try:
        from agent_system.infra.models import Recipe as _R

        with session_scope(factory) as db:
            row = db.get(_R, str(recipe.get("recipe_id") or recipe.get("id") or ""))
            if row is not None:
                row.owner_user_id = owner_id(principal)
    except Exception:
        pass
    return recipe


@authenticated_features.get("/recipes")
def list_recipes(
    request: Request, principal: Annotated[Any, Depends(get_principal)]
) -> list[dict[str, Any]]:
    from agent_system.services.recipes import RecipeEngine

    factory = request.app.state.session_factory
    rows = RecipeEngine(_bus(request)).list_recipes(factory)
    # Owner filter when the engine does not scope itself.
    try:
        from agent_system.infra.models import Recipe as _R

        with session_scope(factory) as db:
            allowed = {
                r.id
                for r in apply_owner_filter(db.query(_R), _R, principal).all()
            }
        return [r for r in rows if str(r.get("recipe_id") or r.get("id")) in allowed]
    except Exception:
        return rows


@authenticated_features.get("/recipes/{recipe_id}")
def get_recipe(
    recipe_id: str,
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
) -> dict[str, Any]:
    from agent_system.services.recipes import RecipeEngine

    factory = request.app.state.session_factory
    try:
        from agent_system.infra.models import Recipe as _R

        with session_scope(factory) as db:
            enforce_owner_row(db.get(_R, recipe_id), principal, "recipe")
    except HTTPException:
        raise
    except Exception:
        pass
    recipe = RecipeEngine(_bus(request)).get_recipe(factory, recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    return recipe


class RecipeExecute(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict, max_length=20)


@authenticated_features.post("/recipes/{recipe_id}/execute")
def execute_recipe(
    recipe_id: str,
    body: RecipeExecute,
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
) -> dict[str, Any]:
    from agent_system.services.recipes import RecipeEngine, RecipeError

    factory = request.app.state.session_factory
    try:
        from agent_system.infra.models import Recipe as _R

        with session_scope(factory) as db:
            enforce_owner_row(db.get(_R, recipe_id), principal, "recipe")
    except HTTPException:
        raise
    except Exception:
        pass
    engine = RecipeEngine(_bus(request))
    try:
        result = engine.execute(factory, recipe_id, body.params)
    except RecipeError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=status, detail="recipe error") from exc
    _emit(request, "recipe.started", "recipe", {"recipe_id": recipe_id, **result})
    return result


@authenticated_features.post("/recipes/{recipe_id}/cancel")
def cancel_recipe_run(
    recipe_id: str,
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
) -> dict[str, Any]:
    from agent_system.services.recipes import RecipeEngine, RecipeError

    factory = request.app.state.session_factory
    try:
        from agent_system.infra.models import Recipe as _R

        with session_scope(factory) as db:
            enforce_owner_row(db.get(_R, recipe_id), principal, "recipe")
    except HTTPException:
        raise
    except Exception:
        pass
    engine = RecipeEngine(_bus(request))
    try:
        result = engine.cancel_run(factory, recipe_id)
    except RecipeError as exc:
        raise HTTPException(status_code=409, detail="recipe error") from exc
    return result


# ---------------------------------------------------------------------------
# Phase 17 — Personality (versioned, security-bounded)
# ---------------------------------------------------------------------------


class PersonalityUpdate(BaseModel):
    tone: str | None = Field(default=None, pattern=r"^(formal|casual|concise|friendly)$")
    verbosity: int | None = Field(default=None, ge=1, le=10)
    reasoning_style: str | None = Field(default=None, pattern=r"^(careful|fast|detailed|balanced)$")
    system_prompt_override: str | None = Field(default=None, max_length=8000)


@authenticated_features.get("/personality/{agent_id}")
def get_personality(agent_id: str, request: Request) -> dict[str, Any]:
    from agent_system.services.personality import PersonalityManager

    factory = request.app.state.session_factory
    mgr = PersonalityManager(factory)
    record = mgr.get(agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no personality configured")
    return record


@authenticated_features.put("/personality/{agent_id}")
def update_personality(agent_id: str, body: PersonalityUpdate, request: Request) -> dict[str, Any]:
    from agent_system.services.personality import PersonalityManager

    factory = request.app.state.session_factory
    mgr = PersonalityManager(factory)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    return mgr.update(agent_id, updates)


class FeedbackCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=4000)
    session_id: str | None = None


@authenticated_features.post("/personality/{agent_id}/feedback", status_code=201)
def submit_feedback(agent_id: str, body: FeedbackCreate, request: Request) -> dict[str, Any]:
    from agent_system.services.personality import PersonalityManager

    factory = request.app.state.session_factory
    mgr = PersonalityManager(factory)
    result = mgr.record_feedback(agent_id, body.rating, body.comment, body.session_id)
    return result


@authenticated_features.post("/personality/{agent_id}/learn")
def learn_personality(agent_id: str, request: Request) -> dict[str, Any]:
    """Feedback loop: N>=10 ratings -> inferred adjustments (prompt-only)."""
    from agent_system.services.personality import PersonalityManager

    factory = request.app.state.session_factory
    mgr = PersonalityManager(factory)
    return mgr.learn_from_feedback(agent_id)


# ---------------------------------------------------------------------------
# Phase 17 — Insights (event-derived, redacted)
# ---------------------------------------------------------------------------


class InsightGenerate(BaseModel):
    insight_type: str = Field(default="daily", pattern=r"^(daily|weekly|anomaly)$")


@authenticated_features.post("/insights/generate")
def generate_insight(body: InsightGenerate, request: Request) -> dict[str, Any]:
    from agent_system.services.insights import InsightGenerator

    factory = request.app.state.session_factory
    gen = InsightGenerator(factory, _bus(request))
    return gen.generate(body.insight_type)


@authenticated_features.get("/insights")
def list_insights(
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        query = apply_owner_filter(db.query(Insight), Insight, principal)
        if not include_archived:
            query = query.filter(Insight.archived_at.is_(None))
        rows = query.order_by(Insight.generated_at.desc()).limit(100).all()
        return [
            {
                "insight_id": r.id,
                "insight_type": r.insight_type,
                "generated_at": r.generated_at.isoformat(),
                "key_findings": r.key_findings_json,
                "content": r.content_html,
                "archived": r.archived_at is not None,
            }
            for r in rows
        ]


@authenticated_features.post("/insights/{insight_id}/archive")
def archive_insight(
    insight_id: str,
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
) -> dict[str, Any]:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        row = enforce_owner_row(db.get(Insight, insight_id), principal, "insight")
        row.archived_at = utcnow()
        return {"insight_id": insight_id, "archived": True}


# ---------------------------------------------------------------------------
# Phase 17 — Scheduler (daily/weekly insight jobs)
# ---------------------------------------------------------------------------
# NOTE: scheduled jobs are currently STORED ONLY — there is no background
# executor wired to fire them (no fake execution, no fabricated runs).
# TODO: wire an APScheduler/dispatcher that reads ScheduledJob rows and
# creates goal sessions on schedule. Until then the API exposes a computed
# `next_run` hint (best-effort, informational only) alongside the stored row.


def _compute_next_run(kind: str, schedule: dict[str, Any]) -> str | None:
    """Best-effort next-run hint for a stored schedule (None when unknown).

    Supports interval ({seconds/minutes/hours}), date ({run_at: ISO}), and
    cron ({cron: "m h * * *"} or crontab fields). Never schedules or fires
    anything — purely informational.
    """
    from datetime import UTC, datetime, timedelta

    try:
        if kind == "interval":
            secs = float(schedule.get("seconds", 0) or 0)
            secs += float(schedule.get("minutes", 0) or 0) * 60
            secs += float(schedule.get("hours", 0) or 0) * 3600
            if secs <= 0:
                return None
            return (datetime.now(UTC) + timedelta(seconds=secs)).isoformat()
        if kind == "date":
            run_at = schedule.get("run_at") or schedule.get("at")
            return str(run_at) if run_at else None
        if kind == "cron":
            expr = schedule.get("cron") or schedule.get("crontab", "")
            if not expr:
                # cron dict-style fields without an expression: cannot compute.
                return None
            try:
                from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
            except Exception:
                return None
            now = datetime.now(UTC)
            trigger = CronTrigger.from_crontab(str(expr), timezone="UTC")
            nxt = trigger.get_next_fire_time(None, now)
            return nxt.isoformat() if nxt is not None else None
    except Exception:
        return None
    return None


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(pattern=r"^(cron|interval|date|webhook)$")
    schedule: dict[str, Any]
    payload: dict[str, Any] = Field(default_factory=dict)


@authenticated_features.post("/schedule", status_code=201)
def create_scheduled_job(
    body: ScheduleCreate,
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
) -> dict[str, Any]:
    if len(str(body.schedule)) > 10 * 1024 or len(str(body.payload)) > 10 * 1024:
        raise HTTPException(status_code=413, detail="schedule/payload too large")
    factory = request.app.state.session_factory
    job_id = ids.new_id("job")
    with session_scope(factory) as db:
        db.add(
            ScheduledJob(
                id=job_id,
                name=body.name,
                kind=body.kind,
                schedule_json=body.schedule,
                payload_json=body.payload,
                owner_user_id=owner_id(principal),
            )
        )
    return {
        "job_id": job_id,
        "name": body.name,
        "enabled": True,
        "next_run": _compute_next_run(body.kind, body.schedule),
        "note": "stored only — no executor wired yet (see module note)",
    }


@authenticated_features.get("/schedule")
def list_scheduled_jobs(
    request: Request, principal: Annotated[Any, Depends(get_principal)]
) -> list[dict[str, Any]]:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        rows = apply_owner_filter(
            db.query(ScheduledJob).order_by(ScheduledJob.created_at.desc()),
            ScheduledJob,
            principal,
        ).all()
        return [
            {
                "job_id": r.id,
                "name": r.name,
                "kind": r.kind,
                "schedule": r.schedule_json,
                "payload": r.payload_json,
                "enabled": r.enabled,
                "next_run": _compute_next_run(r.kind, r.schedule_json or {}),
            }
            for r in rows
        ]


@authenticated_features.delete("/schedule/{job_id}", status_code=204)
def delete_scheduled_job(
    job_id: str,
    request: Request,
    principal: Annotated[Any, Depends(get_principal)],
) -> None:
    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        row = enforce_owner_row(db.get(ScheduledJob, job_id), principal, "job")
        db.delete(row)


# ---------------------------------------------------------------------------
# Phase 18 — Autopilot (off by default; kill switch; audit)
# ---------------------------------------------------------------------------


@authenticated_features.get("/autopilot/status")
def autopilot_status(request: Request) -> dict[str, Any]:
    svc: Any = getattr(request.app.state, "autopilot", None)
    if svc is None:
        return {
            "enabled": False,
            "killed": False,
            "note": "autopilot not configured on this instance (off by default)",
            "audit": [],
        }
    return {
        "enabled": svc.is_enabled,
        "killed": svc.is_killed(),
        "note": "autopilot requires explicit enable + per-action approvals",
        "audit": svc.audit(),
    }


class AutopilotKillRequest(BaseModel):
    pass


@authenticated_features.post("/autopilot/kill")
def autopilot_kill(request: Request) -> dict[str, Any]:
    svc: Any = getattr(request.app.state, "autopilot", None)
    if svc is not None:
        svc.kill()
    _emit(request, "tool.completed", "user", {"autopilot": "kill_switch_engaged"})
    return {"killed": True}


@authenticated_features.post("/autopilot/reset")
def autopilot_reset(request: Request) -> dict[str, Any]:
    """Re-arm after a kill; the service stays disabled until enabled()."""
    svc: Any = getattr(request.app.state, "autopilot", None)
    if svc is not None:
        svc.reset()
    return {"killed": False, "enabled": bool(svc and svc.is_enabled)}


# ---------------------------------------------------------------------------
# Phase 16/17 support — model calls + qa reports listing (Cost/QA tabs)
# ---------------------------------------------------------------------------


@authenticated_features.get("/model-calls")
def list_model_calls(
    request: Request, task_id: str | None = None, limit: Annotated[int, Query(le=500)] = 100
) -> list[dict[str, Any]]:
    from agent_system.infra.models import ModelCall

    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        query = db.query(ModelCall)
        if task_id:
            query = query.filter_by(task_id=task_id)
        rows = query.order_by(ModelCall.created_at.desc()).limit(limit).all()
        return [
            {
                "id": r.id,
                "task_id": r.task_id,
                "provider": r.provider,
                "model_id": r.model_id,
                "status": r.status,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "tokens_cached": r.tokens_cached,
                "usage_is_estimated": r.usage_is_estimated,
                "cost_usd": r.cost_usd,
                "cost_is_estimated": r.cost_is_estimated,
                "latency_ms": r.latency_ms,
            }
            for r in rows
        ]


@authenticated_features.get("/qa-reports")
def list_qa_reports(
    request: Request, limit: Annotated[int, Query(le=200)] = 50
) -> list[dict[str, Any]]:
    from agent_system.infra.models import QAReport

    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        rows = db.query(QAReport).order_by(QAReport.created_at.desc()).limit(limit).all()
        return [
            {
                "report_id": r.id,
                "task_id": r.task_id,
                "code_file": r.code_file,
                "tests_generated": r.tests_generated,
                "tests_executed": r.tests_executed,
                "tests_passed": r.tests_passed,
                "tests_failed": r.tests_failed,
                "tests_skipped": r.tests_skipped,
                "duration_ms": r.duration_ms,
                "coverage_pct": r.coverage_pct,
            }
            for r in rows
        ]


@authenticated_features.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, request: Request) -> dict[str, Any]:
    from agent_system.infra.models import Artifact

    factory = request.app.state.session_factory
    with session_scope(factory) as db:
        row = db.get(Artifact, artifact_id)
        if row is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return {
            "id": row.id,
            "task_id": row.task_id,
            "kind": row.kind,
            "path": row.path,
            "size_bytes": row.size_bytes,
            "created_at": row.created_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Model routing — provider list + quick test (Phase 19-20 support)
# ---------------------------------------------------------------------------


@authenticated_features.get("/model-routing/providers")
def list_routing_providers(request: Request) -> dict[str, Any]:
    """Show every catalog provider, its configured state, and pricing."""
    from agent_system.services.providers import (
        build_pricing,
        configured_providers,
        default_model_id,
        serialize_pricing,
    )

    settings = get_settings()
    router = getattr(request.app.state, "model_router", None)
    return {
        "default_provider": settings.default_provider,
        "default_model": default_model_id(settings),
        "providers": configured_providers(settings),
        "pricing": serialize_pricing(build_pricing(settings)),
        "router_active": router is not None and bool(getattr(router, "_adapters", {})),
    }


class ProviderTestRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    model: str | None = Field(default=None, max_length=120)
    prompt: str = Field(default="ping", max_length=500)


@authenticated_features.post("/model-routing/test")
def test_model_provider(body: ProviderTestRequest, request: Request) -> dict[str, Any]:
    """Fire a tiny request at a provider to validate keys + headers."""
    from agent_system.services.providers import test_provider

    settings = get_settings()
    result = test_provider(settings, body.provider, model=body.model, prompt=body.prompt)
    return result


@authenticated_features.post("/model-routing/diagnose")
def diagnose_model_provider(body: ProviderTestRequest, request: Request) -> dict[str, Any]:
    """Staged provider self-test: credentials/endpoint/model/completion/stream/tools.

    Identifies the exact failing stage instead of a single "provider failed";
    responses never contain API keys.
    """
    from agent_system.services.providers import diagnose_provider

    settings = get_settings()
    return diagnose_provider(settings, body.provider, model=body.model, prompt=body.prompt)


@authenticated_features.get("/model-routing/diagnostics")
def provider_diagnostics(request: Request) -> dict[str, Any]:
    """Configuration report per provider: configured or missing, never the secret."""
    from agent_system.services.providers import configured_providers, provider_spec

    settings = get_settings()
    providers: dict[str, Any] = {}
    for entry in configured_providers(settings):
        key = entry["key"]
        spec = provider_spec(key)
        env_var = f"{str(key).upper()}_API_KEY"
        if key == "gemini":
            env_var = "GEMINI_API_KEY (or GOOGLE_API_KEY)"
        elif key == "ollama":
            env_var = "— (keyless)"
        providers[key] = {
            "label": entry["label"],
            "configured": entry["configured"],
            "needs_key": entry["needs_key"],
            "env_var": env_var,
            "default_model": spec.default_model if spec else entry.get("default_model"),
            "api_surface": spec.api_surface if spec else "chat",
            "free_tier": entry.get("free_tier", False),
        }
    return {"providers": providers}


# re-exported for type checkers
_ = Decision, Task
