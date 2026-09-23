"""FastAPI application entry point (v3.1 §32: /health and /ready).

App state carries the real services (authenticator, permission gate, session
factory, event bus) that routers depend on. Phase 3+
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_system.api.deps import get_authenticator
from agent_system.config import get_settings
from agent_system.infra.db import make_engine, make_session_factory
from agent_system.infra.event_bus import EventBus
from agent_system.services.auth import Authenticator
from agent_system.services.permissions import PermissionGate

_engine = None
_session_factory = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _engine, _session_factory
    settings = get_settings()
    # Telemetry (optional): unset endpoint => fully disabled, zero overhead.
    from agent_system.infra.telemetry import setup_telemetry

    app.state.telemetry = setup_telemetry(settings, app)
    _engine = make_engine(settings.database_url)
    _session_factory = make_session_factory(_engine)
    app.state.settings = settings
    app.state.session_factory = _session_factory
    app.state.event_bus = EventBus()
    # One authoritative permission gate, backed by the durable approvals table:
    # an approval granted through /api/v1/approvals is therefore visible to the
    # capability that requested it (and survives a restart).
    app.state.gate = PermissionGate(factory=_session_factory)
    app.state.authenticator = Authenticator(settings.api_session_secret)
    # Phase 18: autopilot exists but is OFF unless explicitly enabled.
    from agent_system.services.autopilot import AutopilotService

    app.state.autopilot = AutopilotService(app.state.gate, enabled=False)
    # Telegram gateway (optional): boots only when a bot token is configured.
    from agent_system.services.telegram import TelegramService

    app.state.telegram = TelegramService(
        settings, _session_factory, app.state.gate, app.state.event_bus
    )
    try:
        await app.state.telegram.start()
    except Exception as exc:
        import logging as _logging

        _logging.getLogger(__name__).warning("Telegram service startup error: %s", exc)
        app.state.telegram_startup_error = str(exc)
    # Pure-cloud startup summary (no CLI needed): one log line, no secrets.
    try:
        import logging as _logging2

        from agent_system.services.settings_store import cloud_doctor as _cloud_doctor

        summary = _cloud_doctor(settings)
        _logging2.getLogger(__name__).warning(
            "cloud boot env=%s identity=%s transport=%s telegram=%s webhook_url=%s "
            "allowlist=%s providers=%s default=%s db=%s",
            summary.get("agent_env"),
            summary.get("identity_mode"),
            summary.get("transport"),
            summary.get("telegram_configured"),
            summary.get("webhook_url_set"),
            summary.get("allowlist_count"),
            summary.get("providers_configured"),
            summary.get("default_provider"),
            summary.get("database"),
        )
        for note in summary.get("notes", []):
            _logging2.getLogger(__name__).warning("cloud boot note: %s", note)
    except Exception:
        pass
    # Skills (Hermes-style pluggable capabilities): always available, even
    # with zero skills on disk — discovery degrades to an empty list.
    # Shipped seeds count as "builtin" only when the default dir is in use.
    from pathlib import Path as _Path

    import agent_system
    from agent_system.services.skills import SkillManager

    shipped = _Path(agent_system.__file__).resolve().parents[2] / "skills"
    builtin_dir = shipped if _Path(str(settings.skills_dir)) == _Path("skills") else None
    app.state.skill_manager = SkillManager(settings.skills_dir, builtin_dir=builtin_dir)
    # Soul (identity block for every model call; missing file = no block).
    from agent_system.services.soul import load_soul

    app.state.soul_path, app.state.soul_text = load_soul(settings.soul_path or None)
    # Model router (optional): registers adapters for every configured provider.
    from agent_system.services.providers import build_model_router, configured_providers

    configured = [p["key"] for p in configured_providers(settings) if p["configured"]]
    app.state.model_router = build_model_router(
        app.state.event_bus,
        settings,
        provider_names=configured,
        skill_manager=app.state.skill_manager,
        soul_text=app.state.soul_text or None,
    )
    # A2A handoff (off by default; per-target approvals when enabled).
    from agent_system.services.a2a import A2AService

    app.state.a2a = A2AService(settings, app.state.gate, _session_factory, app.state.event_bus)
    # Backup cadence (APScheduler, gated by scheduler_enabled): SQLite online
    # copy + vault/recordings snapshot, daily at 03:00 local by default.
    # Postgres deployments (e.g. Heroku) rely on managed database backups
    # instead — BackupService only supports SQLite, so no job is scheduled.
    app.state.scheduler = None
    _is_sqlite = str(settings.database_url).startswith("sqlite")
    if settings.scheduler_enabled and _is_sqlite:
        try:
            from apscheduler.schedulers.background import (  # type: ignore[import-untyped]
                BackgroundScheduler,
            )
            from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

            from agent_system.services.backup import BackupService

            scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)
            service = BackupService(settings, _session_factory, app.state.event_bus)

            def _backup_job() -> None:
                try:
                    service.run()
                except Exception:
                    pass  # failures are recorded as backup.failed events

            scheduler.add_job(
                _backup_job,
                CronTrigger.from_crontab(
                    settings.backup_schedule_cron,
                    timezone=settings.scheduler_timezone,
                ),
                id="nightly-backup",
                replace_existing=True,
            )
            scheduler.start()
            app.state.scheduler = scheduler
        except Exception:
            # A broken scheduler config must never take down the API server.
            app.state.scheduler = None
    # Startup recovery: tasks left QUEUED by a previous process (restart/
    # crash) are re-kicked in-process — without Redis/RQ the API process is
    # the executor, so this is what keeps a restart from orphaning work.
    # Order matters: recover_orphans requeues tasks whose lease expired, then
    # sweep_backlog kicks everything QUEUED.
    app.state.runner_scheduler = None
    try:
        from agent_system.services.orchestrator import Orchestrator
        from agent_system.services.task_runner import reset_runner, sweep_backlog

        reset_runner()
        if settings.task_runner_recovery_enabled:
            from apscheduler.schedulers.background import BackgroundScheduler

            recovery_factory = _session_factory
            recovery_bus = app.state.event_bus
            recovery_orchestrator = Orchestrator(recovery_bus, settings=settings)

            def recover_tasks() -> None:
                recovery_orchestrator.recover_orphans(recovery_factory)
                sweep_backlog(recovery_factory, recovery_bus, settings=settings)
                # Telegram gateway pipeline recovery: replay relay-able events
                # that never reached the outbox, redrive ledger rows that never
                # reached processed (dyno died mid-task), then drain the
                # delivery outbox. (Telegram Gateway E2E spec)
                try:
                    from agent_system.services.gateway import GatewayExecutor
                    from agent_system.services.outbox import Outbox

                    executor = GatewayExecutor(settings, recovery_factory, recovery_bus)
                    executor.recover(background=True)
                    Outbox(recovery_factory, settings).drain()
                except Exception as exc:
                    import logging as _logging

                    _logging.getLogger(__name__).warning("Telegram recovery sweep error: %s", exc)

            recover_tasks()
            app.state.runner_scheduler = BackgroundScheduler()
            app.state.runner_scheduler.add_job(
                recover_tasks, "interval", seconds=10, max_instances=1, coalesce=True
            )
            app.state.runner_scheduler.start()
    except Exception:
        raise  # never serve requests with an unsafe runner lifecycle
    yield
    try:
        if app.state.scheduler is not None:
            app.state.scheduler.shutdown(wait=False)
    except Exception:
        pass
    try:
        await app.state.telegram.stop()
    except Exception:
        pass
    if app.state.runner_scheduler is not None:
        app.state.runner_scheduler.shutdown(wait=True)
    from agent_system.services.task_runner import shutdown_runner

    shutdown_runner()  # do not dispose the DB beneath surviving runner threads
    if _engine is not None:
        _engine.dispose()


import os as _os

_prod = (_os.environ.get("AGENT_ENV", "") or "").lower() == "production"
app = FastAPI(
    title="Agent System",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _prod else "/docs",
    redoc_url=None if _prod else "/redoc",
    openapi_url=None if _prod else "/openapi.json",
)

# CORS (settings-driven; defaults to the local UI origin). Added after app
# creation so the existing lifespan is untouched.
_cors_settings = get_settings()
_origins = _cors_settings.cors_origins_list or ["http://localhost:3000"]
if "*" in _origins:
    raise RuntimeError("API_CORS_ORIGINS='*' cannot be used with allow_credentials=True")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)


def api_health() -> dict[str, str]:
    return {"status": "ok"}


def api_ready() -> dict[str, Any]:
    from agent_system.services.health import HealthRegistry

    registry = HealthRegistry()
    return registry.check_all(timeout=3.0)


from agent_system.api.v1.a2a import a2a_router  # noqa: E402
from agent_system.api.v1.features import authenticated_features  # noqa: E402
from agent_system.api.v1.realtime import realtime_router  # noqa: E402
from agent_system.api.v1.router import authenticated as v1_authenticated  # noqa: E402
from agent_system.api.v1.router import router as v1_router  # noqa: E402
from agent_system.api.v1.settings import settings_router  # noqa: E402
from agent_system.api.v1.skills import skills_router  # noqa: E402
from agent_system.api.v1.telegram import telegram_router  # noqa: E402

app.include_router(v1_router)
app.include_router(v1_authenticated)
app.include_router(realtime_router)
app.include_router(authenticated_features)
app.include_router(settings_router)
app.include_router(skills_router)
app.include_router(telegram_router)
app.include_router(a2a_router)


@app.get("/api/v1/ready-dependency-check", include_in_schema=False)
def ready_dep(dep: Annotated[Authenticator, Depends(get_authenticator)]) -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/doctor", include_in_schema=False)
def cloud_doctor(dep: Annotated[Authenticator, Depends(get_authenticator)]) -> dict[str, Any]:
    """Cloud equivalent of `agentctl doctor` — no CLI needed, no secrets leaked."""
    from agent_system.services.settings_store import check_settings, cloud_doctor as _doctor

    settings = get_settings()
    body = _doctor(settings)
    body["settings_check"] = check_settings()
    try:
        tg = getattr(app.state, "telegram", None)
        body["telegram_transport"] = tg.transport if tg is not None else None
        body["telegram_startup_error"] = getattr(app.state, "telegram_startup_error", None)
    except Exception:
        pass
    return body
