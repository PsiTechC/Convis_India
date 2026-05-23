"""One-shot, idempotent backfill: align every Convis-managed Twilio number
on every tenant to the unified `/api/twilio-webhooks/voice` webhook.

Equivalent to running `scripts/backfill_unified_voice_routing.py` but exposed
as an admin-gated HTTP endpoint so it can be triggered against the live API
without SSM/ECS shell access. Re-running is a no-op (Twilio's update is
itself idempotent and the helper computes the same target URL each time).

Why this exists
---------------
The unified-voice-routing fix is enforced going forward by the helper at
every entry point (purchase, connect-provider import, refresh,
assign-assistant). But numbers configured BEFORE the fix shipped still carry
their previous voice_url / voice_application_sid. This endpoint walks every
phone_numbers doc with provider=twilio and re-applies the helper, fixing
those carry-overs in one pass.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from twilio.rest import Client

from app.config.database import Database
from app.utils.auth import get_current_user, require_admin
from app.utils.twilio_helpers import (
    CredentialDecryptionError,
    decrypt_twilio_credentials,
)
from app.utils.twilio_voice_routing import (
    ensure_unified_voice_routing,
    unified_voice_url,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class PerNumberResult(BaseModel):
    user_id: str
    phone_number: str
    provider_sid: str
    ok: bool
    message: str


class BackfillResponse(BaseModel):
    target_voice_url: str
    total: int
    updated: int
    failed: int
    skipped: int
    results: List[PerNumberResult]


@router.post(
    "/backfill-twilio-voice-routing",
    response_model=BackfillResponse,
    status_code=status.HTTP_200_OK,
)
async def backfill_twilio_voice_routing(
    dry_run: bool = False,
    current_user: dict = Depends(get_current_user),
):
    """Walk every Twilio number under management and ensure its voice_url
    points at /api/twilio-webhooks/voice (clearing voice_application_sid).

    Admin-only. Idempotent. Per-number failures are reported in the response
    body and do not abort the loop — one Twilio API blip on number 27 of 50
    still fixes numbers 1-26 and 28-50.

    Query params:
      dry_run: If true, decrypt creds and tally what WOULD change without
               touching Twilio. Useful as a safety check before the real run.
    """
    require_admin(current_user)

    target_url = unified_voice_url()
    if not target_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "API_BASE_URL is unset on this API instance — refusing to "
                "backfill (we'd write an invalid voice_url). Set the env var "
                "and redeploy before re-running."
            ),
        )

    db = Database.get_db()
    phone_numbers_collection = db["phone_numbers"]
    provider_connections_collection = db["provider_connections"]

    twilio_phones: List[Dict[str, Any]] = list(
        phone_numbers_collection.find(
            {"provider": "twilio", "provider_sid": {"$exists": True, "$ne": None}},
            {"_id": 1, "user_id": 1, "phone_number": 1, "provider_sid": 1},
        )
    )

    # Group by owning user so we open one Twilio Client per tenant rather
    # than per number — the SDK pools its HTTP connections per-Client.
    by_user: Dict[ObjectId, List[Dict[str, Any]]] = {}
    for p in twilio_phones:
        by_user.setdefault(p["user_id"], []).append(p)

    results: List[PerNumberResult] = []
    summary = {"updated": 0, "failed": 0, "skipped": 0}

    for user_obj_id, phones in by_user.items():
        conn = provider_connections_collection.find_one(
            {"user_id": user_obj_id, "provider": "twilio"}
        )
        if not conn:
            for p in phones:
                results.append(PerNumberResult(
                    user_id=str(user_obj_id),
                    phone_number=p.get("phone_number", ""),
                    provider_sid=p["provider_sid"],
                    ok=False,
                    message="no provider_connections doc for owning user — skipped",
                ))
                summary["skipped"] += 1
            continue

        try:
            account_sid, auth_token = decrypt_twilio_credentials(conn)
        except CredentialDecryptionError as exc:
            for p in phones:
                results.append(PerNumberResult(
                    user_id=str(user_obj_id),
                    phone_number=p.get("phone_number", ""),
                    provider_sid=p["provider_sid"],
                    ok=False,
                    message=f"credential decryption failed: {exc}",
                ))
                summary["skipped"] += 1
            continue

        if not (account_sid and auth_token):
            for p in phones:
                results.append(PerNumberResult(
                    user_id=str(user_obj_id),
                    phone_number=p.get("phone_number", ""),
                    provider_sid=p["provider_sid"],
                    ok=False,
                    message="stored Twilio credentials are blank",
                ))
                summary["skipped"] += 1
            continue

        if dry_run:
            for p in phones:
                results.append(PerNumberResult(
                    user_id=str(user_obj_id),
                    phone_number=p.get("phone_number", ""),
                    provider_sid=p["provider_sid"],
                    ok=True,
                    message=f"WOULD update voice_url -> {target_url}",
                ))
                summary["updated"] += 1
            continue

        client = Client(account_sid, auth_token)
        for p in phones:
            label = p.get("phone_number", p["provider_sid"])
            ok, msg = ensure_unified_voice_routing(
                client, p["provider_sid"], label=label,
            )
            results.append(PerNumberResult(
                user_id=str(user_obj_id),
                phone_number=p.get("phone_number", ""),
                provider_sid=p["provider_sid"],
                ok=ok,
                message=msg,
            ))
            if ok:
                summary["updated"] += 1
            else:
                summary["failed"] += 1

    logger.info(
        "[ADMIN_BACKFILL] %s. total=%d updated=%d failed=%d skipped=%d "
        "triggered_by=%s",
        "dry-run" if dry_run else "live",
        len(twilio_phones), summary["updated"], summary["failed"], summary["skipped"],
        current_user.get("user_id"),
    )

    return BackfillResponse(
        target_voice_url=target_url,
        total=len(twilio_phones),
        updated=summary["updated"],
        failed=summary["failed"],
        skipped=summary["skipped"],
        results=results,
    )
