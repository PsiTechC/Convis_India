"""LiveKit SIP integration — create outbound SIP participants that dial PSTN
numbers via Twilio Elastic SIP Trunking, and track inbound SIP calls.

Requires: LIVEKIT_SIP_OUTBOUND_TRUNK_ID pointing at a trunk configured in
LiveKit Cloud whose termination URI is the user's Twilio SIP trunk.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any, Dict, Optional

from livekit import api

from app.config.settings import settings
from app.services.livekit.assistant_config import encode_metadata
from app.services.livekit.tokens import LiveKitNotConfigured, livekit_api_client

logger = logging.getLogger(__name__)


def generate_room_name(prefix: str = "call") -> str:
    return f"{prefix}-{secrets.token_urlsafe(8)}"


async def create_room_with_agent(
    *,
    room_name: str,
    assistant_config: Dict[str, Any],
    metadata_extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Create a LiveKit room with encoded assistant metadata and dispatch the
    agent worker into it. Returns the room name."""
    if not settings.livekit_agent_name:
        raise LiveKitNotConfigured("LIVEKIT_AGENT_NAME is required")

    metadata = dict(assistant_config)
    if metadata_extra:
        metadata.update(metadata_extra)

    lk = livekit_api_client()
    try:
        await lk.room.create_room(
            api.CreateRoomRequest(name=room_name, metadata=encode_metadata(metadata))
        )
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=settings.livekit_agent_name,
                room=room_name,
                metadata=encode_metadata(metadata),
            )
        )
    finally:
        await lk.aclose()

    logger.info("[LIVEKIT] Room %s created and agent dispatched", room_name)
    return room_name


async def dial_outbound_sip(
    *,
    room_name: str,
    phone_number: str,
    caller_id: Optional[str] = None,
    participant_identity: str = "caller",
    participant_name: Optional[str] = None,
    trunk_id: Optional[str] = None,
) -> str:
    """Ask LiveKit SIP to place an outbound call that lands in `room_name`.

    `trunk_id` selects which LiveKit outbound trunk to dial through (one per
    PSTN provider — Twilio, Vobiz, etc). If not provided, falls back to the
    global LIVEKIT_SIP_OUTBOUND_TRUNK_ID env (legacy single-provider setup).

    Returns the SIP participant identity.
    """
    effective_trunk_id = trunk_id or settings.livekit_sip_outbound_trunk_id
    if not effective_trunk_id:
        raise LiveKitNotConfigured(
            "No outbound SIP trunk configured: pass trunk_id explicitly or set "
            "LIVEKIT_SIP_OUTBOUND_TRUNK_ID env var"
        )

    lk = livekit_api_client()
    try:
        request = api.CreateSIPParticipantRequest(
            sip_trunk_id=effective_trunk_id,
            sip_call_to=phone_number,
            room_name=room_name,
            participant_identity=participant_identity,
            participant_name=participant_name or phone_number,
            play_dialtone=False,
        )
        if caller_id:
            request.sip_number = caller_id
        await lk.sip.create_sip_participant(request)
    finally:
        await lk.aclose()

    logger.info(
        "[LIVEKIT SIP] Dialing %s via trunk %s into room %s",
        phone_number, effective_trunk_id, room_name,
    )
    return participant_identity


async def hangup_room(room_name: str) -> None:
    lk = livekit_api_client()
    try:
        await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
    finally:
        await lk.aclose()
    logger.info("[LIVEKIT] Room %s deleted (hangup)", room_name)


async def transfer_twilio_call_to_number(
    *,
    call_sid: str,
    target_number: str,
    owner_user_id: Any,
    direction: str = "inbound",
) -> bool:
    """Redirect a live Twilio PSTN call (`call_sid`) to `target_number` by
    replacing the call's TwiML with `<Dial><Number>`. This drops whatever the
    call was doing (the `<Dial><Sip>` bridge into LiveKit) and connects the
    caller to the human number.

    Soft-fails: returns ``False`` on ANY problem (missing creds, bad call_sid,
    Twilio API error) — the caller maps that to "transfer not available" and
    keeps the AI conversation going. Never raises.

    When `settings.api_base_url`/`base_url` is set, the `<Dial>` carries an
    `action` callback to `/api/twilio-webhooks/transfer-result?dir=<direction>`
    so a no-answer/busy/failed outcome can hand the call back to the AI.
    """
    # Re-validate the target as strict E.164 here (defence-in-depth): the value
    # is already coerced upstream, but this function hand-builds TwiML XML, so a
    # non-E.164 value must never reach the f-string (TwiML/XML injection = call
    # control). E.164 by construction contains no XML-special chars.
    import re as _re
    if not call_sid or not target_number or not _re.fullmatch(r"\+[1-9]\d{1,14}", target_number):
        logger.warning("[TRANSFER] declined: call_sid=%r target=%r (not E.164?)", call_sid, target_number)
        return False
    # `direction` is internal ("inbound"/"outbound") but URL-quote it anyway so
    # it can never break out of the query string in the action callback.
    from urllib.parse import quote as _urlquote
    direction_q = _urlquote((direction or "inbound"), safe="")

    # Everything below is synchronous (pymongo + Twilio REST + crypto) — run it
    # entirely off the event loop so the agent's audio pipeline never stalls.
    def _resolve_and_redirect() -> bool:
        from app.config.database import Database
        from app.utils.twilio_helpers import (
            decrypt_twilio_credentials,
            CredentialDecryptionError,
        )
        from bson import ObjectId

        db = Database.get_db()
        conn = None
        if owner_user_id is not None:
            # The user_id in provider_connections may be stored as ObjectId or str.
            queries = []
            try:
                queries.append({"user_id": ObjectId(str(owner_user_id)), "provider": "twilio"})
            except Exception:
                pass
            queries.append({"user_id": str(owner_user_id), "provider": "twilio"})
            for q in queries:
                conn = db["provider_connections"].find_one(q)
                if conn:
                    break

        account_sid = auth_token = None
        if conn:
            try:
                account_sid, auth_token = decrypt_twilio_credentials(conn)
            except CredentialDecryptionError as exc:
                logger.warning("[TRANSFER] cred decryption failed for user %s: %s", owner_user_id, exc)
                account_sid = auth_token = None
        if not (account_sid and auth_token):
            account_sid = os.getenv("TWILIO_ACCOUNT_SID")
            auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        if not (account_sid and auth_token):
            logger.warning("[TRANSFER] no Twilio credentials available for user %s", owner_user_id)
            return False

        base = settings.api_base_url or settings.base_url
        action_attr = ""
        if base:
            cb = f"{base.rstrip('/')}/api/twilio-webhooks/transfer-result?dir={direction_q}"
            action_attr = f' action="{cb}" method="POST"'
        # answerOnBridge: the caller hears ringing (not silence) until the human
        # picks up. timeout=25s before we consider it a no-answer.
        twiml = (
            f'<Response><Dial answerOnBridge="true" timeout="25"{action_attr}>'
            f'<Number>{target_number}</Number></Dial></Response>'
        )
        from twilio.rest import Client
        Client(account_sid, auth_token).calls(call_sid).update(twiml=twiml)
        return True

    try:
        ok = await asyncio.to_thread(_resolve_and_redirect)
        if ok:
            logger.info("[TRANSFER] redirected call %s -> %s (dir=%s)", call_sid, target_number, direction)
        return ok
    except Exception:
        logger.warning("[TRANSFER] failed to redirect call %s", call_sid, exc_info=True)
        return False
