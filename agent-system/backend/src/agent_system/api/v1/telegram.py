"""Telegram gateway API (webhook transport + management endpoints).

- POST /api/v1/telegram/webhook : receive Telegram updates when webhook mode
  is enabled; authenticated via the standard `X-Telegram-Bot-Api-Secret-Token`
  or `X-Telegram-Webhook-Secret` header.
- GET  /api/v1/telegram/status   : report transport / configured state.

Unauthenticated webhook uses a separate secret; management endpoints require
the normal authenticator.
"""

from __future__ import annotations

import hmac as _hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from agent_system.api.deps import get_authenticator

_logger = logging.getLogger(__name__)

telegram_router = APIRouter(prefix="/api/v1/telegram")


def _service(request: Request) -> Any:
    svc = getattr(request.app.state, "telegram", None)
    if svc is None or not svc.is_configured():
        raise HTTPException(status_code=503, detail="telegram gateway not configured")
    return svc


@telegram_router.post("/webhook")
async def telegram_webhook(request: Request) -> dict[str, str]:
    """Receive a Telegram update (webhook mode). Authenticated by header."""
    svc = getattr(request.app.state, "telegram", None)
    if svc is None or not svc.is_configured():
        raise HTTPException(status_code=503, detail="telegram gateway not configured")
    settings = request.app.state.settings
    expected = settings.telegram_webhook_secret
    if not expected:
        raise HTTPException(status_code=400, detail="webhook mode not enabled")
    provided = (
        request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        or request.headers.get("X-Telegram-Webhook-Secret")
        or ""
    )
    if not _hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    try:
        update: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None
    update_id = update.get("update_id")
    from_chat = (update.get("message") or {}).get("chat", {}).get("id")
    _logger.info("telegram.webhook.received update_id=%s chat_id=%s", update_id, from_chat)
    await svc.handle_update(update)
    return {"status": "ok"}


@telegram_router.get("/status", dependencies=[Depends(get_authenticator)])
async def telegram_status(request: Request) -> dict[str, Any]:
    """Report whether the gateway is configured and its transport mode."""
    svc = getattr(request.app.state, "telegram", None)
    if svc is None or not svc.is_configured():
        return {"configured": False, "transport": None}
    return {
        "configured": True,
        "transport": svc.transport,
        "allowed_chat_ids": sorted(svc._allowed),  # noqa: SLF001
    }
