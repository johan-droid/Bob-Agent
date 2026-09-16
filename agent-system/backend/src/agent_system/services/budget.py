"""Budget accounting derived from persisted records (v3.1 §12).

    daily · session · task · provider

The database is authoritative. Spend is *derived* from the ``model_calls``
table — the durable record of every model invocation — instead of a
process-local counter, which is what makes these guarantees true:

- a restart cannot reset usage;
- the API process and the RQ worker agree on what has been spent;
- the number a budget enforces is the same number the cost UI shows.

Respects the configured limits: ``MAX_TASK_COST_USD``, ``MAX_TASK_TOKENS`` and
``DAILY_BUDGET_USD``. Every scope reports the same shape (spent / limit /
remaining), and :meth:`BudgetLedger.check` returns a decision plus the reason,
so the router can fail fast with an honest explanation.

The legacy in-memory ``BudgetMonitor`` in ``services/model_router.py`` remains
for alert thresholds, but enforcement reads from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from agent_system.domain.events import utcnow

SCOPE_DAILY = "daily"
SCOPE_SESSION = "session"
SCOPE_TASK = "task"
SCOPE_PROVIDER = "provider"

#: Alert thresholds (percent) reported per scope.
ALERT_LEVELS = (50.0, 75.0, 90.0, 100.0)


@dataclass(frozen=True)
class ScopeUsage:
    """Spend and token usage for one budget scope."""

    scope: str
    key: str | None
    spent_usd: float
    limit_usd: float | None
    tokens: int = 0
    token_limit: int | None = None

    @property
    def remaining_usd(self) -> float | None:
        return None if self.limit_usd is None else self.limit_usd - self.spent_usd

    @property
    def percent(self) -> float | None:
        if not self.limit_usd:
            return None
        return (self.spent_usd / self.limit_usd) * 100.0

    @property
    def exceeded(self) -> bool:
        over_usd = self.limit_usd is not None and self.spent_usd >= self.limit_usd
        over_tokens = self.token_limit is not None and self.tokens >= self.token_limit
        return over_usd or over_tokens

    def alerts(self) -> list[float]:
        pct = self.percent
        return [] if pct is None else [level for level in ALERT_LEVELS if pct >= level]

    def to_json(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "key": self.key,
            "spent_usd": round(self.spent_usd, 6),
            "limit_usd": self.limit_usd,
            "remaining_usd": None if self.remaining_usd is None else round(self.remaining_usd, 6),
            "tokens": self.tokens,
            "token_limit": self.token_limit,
            "percent": None if self.percent is None else round(self.percent, 2),
            "exceeded": self.exceeded,
        }


@dataclass(frozen=True)
class BudgetDecision:
    """Whether a model call may proceed."""

    allowed: bool
    reason: str | None = None
    scope: str | None = None
    usage: ScopeUsage | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "scope": self.scope,
            "usage": self.usage.to_json() if self.usage else None,
        }


def _start_of_day(now: datetime | None = None) -> datetime:
    moment = now or utcnow()
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


class BudgetLedger:
    """Derive spend from ``model_calls`` and enforce the configured limits."""

    def __init__(self, factory: Any, settings: Any = None) -> None:
        self._factory = factory
        self._settings = settings

    # -- configuration ------------------------------------------------------

    def _limit(self, name: str, default: float) -> float:
        return float(getattr(self._settings, name, default) or default)

    @property
    def daily_limit_usd(self) -> float:
        return self._limit("daily_budget_usd", 10.0)

    @property
    def task_limit_usd(self) -> float:
        return self._limit("max_task_cost_usd", 2.0)

    @property
    def task_token_limit(self) -> int:
        return int(getattr(self._settings, "max_task_tokens", 200_000) or 200_000)

    # -- queries ------------------------------------------------------------

    def _sum(self, **filters: Any) -> tuple[float, int]:
        from agent_system.infra.models import ModelCall

        stmt = select(
            func.coalesce(func.sum(ModelCall.cost_usd), 0.0),
            func.coalesce(func.sum(ModelCall.tokens_in), 0)
            + func.coalesce(func.sum(ModelCall.tokens_out), 0),
        )
        for column, value in filters.items():
            if value is None:
                continue
            if column == "since":
                stmt = stmt.where(ModelCall.created_at >= value)
            else:
                stmt = stmt.where(getattr(ModelCall, column) == value)
        with self._factory() as db:
            spent, tokens = db.execute(stmt).one()
        return float(spent or 0.0), int(tokens or 0)

    def daily_usage(self) -> ScopeUsage:
        spent, tokens = self._sum(since=_start_of_day())
        return ScopeUsage(
            scope=SCOPE_DAILY,
            key=None,
            spent_usd=spent,
            limit_usd=self.daily_limit_usd,
            tokens=tokens,
        )

    def session_usage(self, session_id: str) -> ScopeUsage:
        spent, tokens = self._sum(session_id=session_id)
        return ScopeUsage(
            scope=SCOPE_SESSION, key=session_id, spent_usd=spent, limit_usd=None, tokens=tokens
        )

    def task_usage(self, task_id: str) -> ScopeUsage:
        spent, tokens = self._sum(task_id=task_id)
        return ScopeUsage(
            scope=SCOPE_TASK,
            key=task_id,
            spent_usd=spent,
            limit_usd=self.task_limit_usd,
            tokens=tokens,
            token_limit=self.task_token_limit,
        )

    def provider_usage(self, provider: str) -> ScopeUsage:
        spent, tokens = self._sum(provider=provider)
        return ScopeUsage(
            scope=SCOPE_PROVIDER, key=provider, spent_usd=spent, limit_usd=None, tokens=tokens
        )

    def snapshot(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Every applicable scope in one call (used by the cost API/UI)."""
        scopes = [self.daily_usage()]
        if session_id:
            scopes.append(self.session_usage(session_id))
        if task_id:
            scopes.append(self.task_usage(task_id))
        if provider:
            scopes.append(self.provider_usage(provider))
        return {"scopes": [scope.to_json() for scope in scopes]}

    # -- enforcement --------------------------------------------------------

    def check(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        provider: str | None = None,
    ) -> BudgetDecision:
        """Decide whether a model call may proceed, most-specific scope first."""
        if task_id:
            task = self.task_usage(task_id)
            if task.exceeded:
                return BudgetDecision(
                    allowed=False,
                    reason=(
                        f"task budget exhausted (${task.spent_usd:.4f} of "
                        f"${task.limit_usd}, {task.tokens} of {task.token_limit} tokens)"
                    ),
                    scope=SCOPE_TASK,
                    usage=task,
                )
        daily = self.daily_usage()
        if daily.exceeded:
            return BudgetDecision(
                allowed=False,
                reason=(f"daily budget exhausted (${daily.spent_usd:.4f} of ${daily.limit_usd})"),
                scope=SCOPE_DAILY,
                usage=daily,
            )
        return BudgetDecision(allowed=True, scope=SCOPE_DAILY, usage=daily)

    def record_scope_alerts(self, usage: ScopeUsage) -> list[dict[str, Any]]:
        """Thresholds crossed for a usage snapshot (for ``cost.alert`` payloads)."""
        return [
            {
                "scope": usage.scope,
                "key": usage.key,
                "level_pct": level,
                "spent_usd": round(usage.spent_usd, 6),
                "limit_usd": usage.limit_usd,
            }
            for level in usage.alerts()
        ]


__all__ = [
    "ALERT_LEVELS",
    "SCOPE_DAILY",
    "SCOPE_PROVIDER",
    "SCOPE_SESSION",
    "SCOPE_TASK",
    "BudgetDecision",
    "BudgetLedger",
    "ScopeUsage",
]
