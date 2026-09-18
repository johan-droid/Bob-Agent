"""Identity & multi-user isolation (Telegram-first cloud runtime).

The identity chain (spec §9, §17):

    Telegram User -> TelegramAccount -> Bob User -> Role -> Authorization

A Telegram user id is **identity, never authorization**: the mapping to a Bob
user and that user's role decide what a principal may do. Provisioning is
allowlist-gated (``TELEGRAM_ALLOWED_USER_IDS``) and the first provisioned
account becomes ``owner`` so a fresh deployment always has exactly one.

Two modes (:class:`IdentityMode`, config ``AGENT_IDENTITY_MODE``):

- ``local`` — historical single-operator runtime. REST session secret is the
  operator; Telegram keeps its chat-id allowlist behavior. Zero change for
existing local/cloud behavior. Ownership checks resolve to "the operator".
- ``telegram`` — multi-user: every Telegram principal is resolved through
:class:`IdentityService`, ownership columns are enforced server-side, and
the REST API accepts a verified ``?principal=``. Telegram-provided
ownership identifiers are never trusted.

Default posture is **deny**: an unknown/blocked Telegram user is a no-op plus
an audit event, never an error to the sender (no oracle about the bot).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent_system.domain.ids import new_id


class IdentityMode(StrEnum):
    LOCAL = "local"
    TELEGRAM = "telegram"


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    GUEST = "guest"
    BLOCKED = "blocked"


ROLE_POWER: dict[Role, frozenset[Role]] = {
    Role.OWNER: frozenset({Role.OWNER, Role.ADMIN, Role.MEMBER, Role.GUEST}),
    Role.ADMIN: frozenset({Role.ADMIN, Role.MEMBER, Role.GUEST}),
    Role.MEMBER: frozenset({Role.MEMBER}),
    Role.GUEST: frozenset({Role.GUEST}),
    Role.BLOCKED: frozenset(),
}

ROLE_CAPABILITIES: dict[Role, frozenset[str]] = {
    Role.OWNER: frozenset(
        {
            "task.create",
            "task.read",
            "task.cancel",
            "approval.decide",
            "session.create",
            "memory.read",
            "settings.manage",
        }
    ),
    Role.ADMIN: frozenset(
        {
            "task.create",
            "task.read",
            "task.cancel",
            "approval.decide",
            "session.create",
            "memory.read",
        }
    ),
    Role.MEMBER: frozenset(
        {
            "task.create",
            "task.read",
            "task.cancel",
            "approval.decide",
            "session.create",
            "memory.read",
        }
    ),
    Role.GUEST: frozenset({"task.read"}),
    Role.BLOCKED: frozenset(),
}


class IdentityError(PermissionError):
    """Raised when an operation is not permitted for the principal."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Principal:
    """The resolved authorization principal for one request/update."""

    user_id: str | None
    role: Role
    mode: IdentityMode
    chat_id: int | None = None
    telegram_user_id: str | None = None

    @property
    def is_operator(self) -> bool:
        return self.mode is IdentityMode.LOCAL

    @property
    def is_authenticated(self) -> bool:
        return self.is_operator or (self.user_id is not None and self.role is not Role.BLOCKED)

    def can(self, capability: str) -> bool:
        if not self.is_authenticated:
            return False
        return capability in ROLE_CAPABILITIES.get(self.role, frozenset())

    def may_act_on(self, role: Role) -> bool:
        return self.role in ROLE_POWER and role in ROLE_POWER[self.role]


OPERATOR = Principal(user_id=None, role=Role.OWNER, mode=IdentityMode.LOCAL)
"""The local-mode operator: everything allowed, no DB row needed."""


class IdentityService:
    """Resolve Telegram identities to principals; provision allowlisted users."""

    def __init__(self, factory: Any, settings: Any) -> None:
        self._factory = factory
        self._settings = settings

    @property
    def mode(self) -> IdentityMode:
        return IdentityMode(getattr(self._settings, "agent_identity_mode", "local"))

    def allowed_user_ids(self) -> set[str]:
        raw = str(getattr(self._settings, "telegram_allowed_user_ids", "") or "")
        return {p.strip() for p in raw.split(",") if p.strip()}

    def operator(self) -> Principal:
        if self.mode is IdentityMode.LOCAL:
            return OPERATOR
        raise IdentityError("operator principal only exists in local mode")

    def resolve(self, telegram_user_id: str, chat_id: int | None = None) -> Principal | None:
        """Map a Telegram user id to a principal (or None when unmapped).

        Never raises for unknown users — an unmapped identity is a denial,
        not an error surface (no oracle to probing senders).
        """
        if self.mode is IdentityMode.LOCAL:
            return None
        from agent_system.infra.models import TelegramAccount

        with self._factory() as db:
            account = (
                db.query(TelegramAccount)
                .filter_by(telegram_user_id=str(telegram_user_id))
                .one_or_none()
            )
            if account is None:
                return None
            from agent_system.infra.models import User

            user = db.get(User, account.user_id)
            if user is None:
                return None
            if not user.is_active or user.role == Role.BLOCKED.value:
                return None
            role = Role(account.role)
            return Principal(
                user_id=user.id,
                role=role,
                mode=IdentityMode.TELEGRAM,
                chat_id=(int(account.chat_id) if account.chat_id else chat_id),
                telegram_user_id=str(telegram_user_id),
            )

    def provision(
        self,
        telegram_user_id: str,
        display_name: str,
        chat_id: int | None,
    ) -> Principal:
        """Allowlist-gated provisioning of a Telegram user as a Bob user.

        - Unknown telegram user id: refused (deny by default).
        - Blocked account: stays blocked (provisioning never unblocks).
        - Existing account: returned unchanged (idempotent).
        - First provisioned account: becomes ``owner``.
        """
        if self.mode is IdentityMode.LOCAL:
            raise IdentityError("provisioning requires telegram identity mode")
        tid = str(telegram_user_id)
        allowed = self.allowed_user_ids()
        if allowed and tid not in allowed:
            raise IdentityError("telegram user is not on the provisioning allowlist")
        from agent_system.infra.models import TelegramAccount, User

        with self._factory() as db:
            existing = db.query(TelegramAccount).filter_by(telegram_user_id=tid).one_or_none()
            if existing is not None:
                user = existing.user
                if user is None or not user.is_active:
                    raise IdentityError("account is blocked")
                role = Role(existing.role)
                if role is Role.BLOCKED:
                    raise IdentityError("account is blocked")
                return Principal(
                    user_id=user.id,
                    role=role,
                    mode=IdentityMode.TELEGRAM,
                    chat_id=(int(existing.chat_id) if existing.chat_id else chat_id),
                    telegram_user_id=tid,
                )
            first = db.query(User).count() == 0
            user = User(
                id=new_id("usr"),
                display_name=display_name or f"telegram:{tid}",
                auth_provider="telegram",
                role=Role.OWNER.value if first else Role.MEMBER.value,
            )
            db.add(user)
            db.flush()
            account = TelegramAccount(
                id=new_id("tga"),
                telegram_user_id=tid,
                user_id=user.id,
                chat_id=str(chat_id) if chat_id is not None else None,
                role=user.role,
            )
            db.add(account)
            db.flush()
            return Principal(
                user_id=user.id,
                role=Role(user.role),
                mode=IdentityMode.TELEGRAM,
                chat_id=chat_id,
                telegram_user_id=tid,
            )

    def set_role(self, actor: Principal, telegram_user_id: str, role: Role) -> None:
        """Owner/admin role management (never self-demotion of the last owner)."""
        if not actor.can("settings.manage"):
            raise IdentityError("role management requires the settings.manage capability")
        from agent_system.infra.models import TelegramAccount

        with self._factory() as db:
            account = (
                db.query(TelegramAccount)
                .filter_by(telegram_user_id=str(telegram_user_id))
                .one_or_none()
            )
            if account is None:
                raise LookupError("unknown telegram account")
            if not actor.may_act_on(Role(account.role)):
                raise IdentityError("actor cannot manage this principal")
            if role is Role.OWNER and actor.role is not Role.OWNER:
                raise IdentityError("only an owner may grant owner")
            account.role = role.value
            db.flush()
