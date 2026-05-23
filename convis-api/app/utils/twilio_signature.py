"""
Twilio webhook signature verification.

Twilio signs every webhook with HMAC-SHA1 using the account auth token. We
verify on every webhook handler to prevent forged inbound-call status updates,
spoofed caller IDs, free outbound dial triggers, etc.

Usage in a route module:

    from app.utils.twilio_signature import verify_twilio_signature

    @router.post("/voice/incoming", dependencies=[Depends(verify_twilio_signature)])
    async def voice_incoming(...):
        ...

Set TWILIO_VERIFY_WEBHOOKS=0 only for local dev when ngrok signing is fragile;
NEVER in production. The dependency raises 403 on mismatch.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, status
from twilio.request_validator import RequestValidator

from app.config.settings import settings

logger = logging.getLogger(__name__)

# Cache of {twilio_account_sid -> auth_token} resolved from provider_connections,
# so a webhook from a customer's own (BYO) Twilio sub-account can be verified
# with THAT account's auth token rather than only the platform env token.
# Refreshed at most every _ACCT_CACHE_TTL seconds.
_ACCT_CACHE_TTL = 300.0
_acct_token_cache: Dict[str, str] = {}
_acct_cache_loaded_at: float = 0.0


def _resolve_account_auth_token(account_sid: Optional[str]) -> Optional[str]:
    """Best-effort: auth token for `account_sid` from provider_connections.
    Returns None if not found / decryption fails. Synchronous (pymongo + crypto);
    callers run it via asyncio.to_thread."""
    if not account_sid:
        return None
    global _acct_cache_loaded_at
    now = time.monotonic()
    if account_sid not in _acct_token_cache or (now - _acct_cache_loaded_at) > _ACCT_CACHE_TTL:
        try:
            from app.config.database import Database
            from app.utils.twilio_helpers import decrypt_twilio_credentials
            db = Database.get_db()
            fresh: Dict[str, str] = {}
            for conn in db["provider_connections"].find({"provider": "twilio"}):
                try:
                    sid, token = decrypt_twilio_credentials(conn)
                except Exception:
                    continue
                if sid and token:
                    fresh[sid] = token
            _acct_token_cache.clear()
            _acct_token_cache.update(fresh)
            _acct_cache_loaded_at = now
        except Exception:
            logger.debug("[TWILIO] account-token cache refresh failed", exc_info=True)
    return _acct_token_cache.get(account_sid)


def _verification_enabled() -> bool:
    """In production we always verify. Elsewhere, allow opt-out only via an
    explicit env var so tests/local-dev can run without forging signatures."""
    if settings.environment == "production":
        return True
    flag = os.getenv("TWILIO_VERIFY_WEBHOOKS")
    if flag is None:
        # Default-on outside prod too, unless explicitly disabled.
        return True
    return flag not in ("0", "false", "False", "no", "off")


def _public_url(request: Request) -> str:
    """Build the URL Twilio used to call us. Behind a proxy/load balancer
    we must use the X-Forwarded-* headers — never request.url directly."""
    # Prefer the configured public base if set (most reliable behind ngrok/LB)
    base = settings.api_base_url or settings.base_url
    if base:
        return base.rstrip("/") + request.url.path + (
            f"?{request.url.query}" if request.url.query else ""
        )

    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if forwarded_proto and forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}{request.url.path}" + (
            f"?{request.url.query}" if request.url.query else ""
        )
    return str(request.url)


async def verify_twilio_signature(request: Request) -> None:
    """FastAPI dependency: verify X-Twilio-Signature against the request.

    Raises 403 if the signature is missing, malformed, or doesn't match.
    Reads the form body once and stashes it on request.state._twilio_form so
    downstream Form(...) handlers don't have to re-read.
    """
    if not _verification_enabled():
        logger.warning(
            "[TWILIO] Signature verification disabled by TWILIO_VERIFY_WEBHOOKS — "
            "this is only safe for local dev"
        )
        return

    signature = request.headers.get("x-twilio-signature") or request.headers.get(
        "X-Twilio-Signature"
    )
    if not signature:
        logger.warning("[TWILIO] Missing X-Twilio-Signature header on %s", request.url.path)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    # Twilio signs the URL + sorted form params. Read the form body and
    # cache it for downstream handlers.
    form = await request.form()
    params = {k: v for k, v in form.multi_items()}
    request.state._twilio_form = form

    # Candidate auth tokens, tried in order:
    #  1. the token for the AccountSid in this webhook (a customer's BYO Twilio
    #     sub-account places per-user calls — incl. our call-transfer redirect —
    #     so the callback is signed with THAT account's token, not ours);
    #  2. the platform env token (single-tenant deploys, and our own number).
    candidate_tokens: List[str] = []
    webhook_account_sid = params.get("AccountSid") or request.query_params.get("AccountSid")
    if webhook_account_sid and webhook_account_sid != (settings.twilio_account_sid or ""):
        try:
            tok = await asyncio.to_thread(_resolve_account_auth_token, webhook_account_sid)
        except Exception:
            tok = None
        if tok:
            candidate_tokens.append(tok)
    if settings.twilio_auth_token:
        candidate_tokens.append(settings.twilio_auth_token)

    if not candidate_tokens:
        logger.error("[TWILIO] Cannot verify signature: no auth token available "
                     "(env unset and AccountSid=%s not in provider_connections)", webhook_account_sid)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Twilio auth not configured on server",
        )

    url = _public_url(request)
    if not any(RequestValidator(tok).validate(url, params, signature) for tok in candidate_tokens):
        logger.warning(
            "[TWILIO] Signature mismatch on %s (url=%s, account=%s, %d candidate token(s))",
            request.url.path, url, webhook_account_sid, len(candidate_tokens),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
