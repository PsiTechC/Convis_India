"""Outbound calls — PSTN dialed via LiveKit SIP trunk OR Twilio TwiML bridge.

Transport is chosen per-call by the assigned phone number's `provider` field:

- provider != "twilio" (e.g. "vobiz"):
    LiveKit places the SIP call directly via the trunk in
    phone_number.livekit_outbound_trunk_id. Cheaper for India / Asia-Pac.
- provider == "twilio" (default):
    Backend asks Twilio to dial PSTN; on answer, Twilio bridges via SIP to
    LiveKit. Needs only TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN.

Inbound symmetry: the assigned phone number determines which provider handles
the call. Each provider has its own LiveKit inbound trunk + dispatch rule that
routes incoming SIP into the matching assistant's room.

Authentication: every endpoint here requires a valid JWT and only operates on
resources belonging to the JWT's user. We never trust user_id from the URL.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.config.database import Database
from app.config.settings import settings
from app.middleware.rate_limiter import limiter
from app.models.outbound_calls import (
    CheckNumberResponse,
    OutboundCallRequest,
    OutboundCallResponse,
)
from app.services.livekit.assistant_config import load_assistant_config
from app.services.livekit.sip_service import (
    create_room_with_agent,
    dial_outbound_sip,
    generate_room_name,
    hangup_room,
)
from app.services.livekit.tokens import LiveKitNotConfigured
from app.services.twilio_outbound import (
    TwilioNotConfigured,
    dial_outbound_via_twilio,
    hangup_twilio_call,
)
from app.utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

E164 = re.compile(r"^\+[1-9]\d{1,14}$")


def _outbound_transport_for(phone_number_doc: dict) -> str:
    """Pick transport based on the assigned phone number's provider.

    - "twilio" (or missing): Twilio Programmable Voice + TwiML bridge.
    - anything else (e.g. "vobiz"): LiveKit places SIP directly through the
      trunk recorded on the phone_number doc (or env fallback).
    """
    provider = (phone_number_doc.get("provider") or "twilio").lower()
    if provider == "twilio":
        return "twilio-twiml"
    # Non-Twilio providers go via LiveKit-direct SIP trunk.
    return "livekit-sip"


@router.get("/", response_class=JSONResponse)
async def outbound_calls_index():
    """Public health endpoint — does not leak which transport is active in
    detail. Just whether the endpoint is reachable."""
    return {"message": "Outbound calls service is running"}


@router.post(
    "/make-call/{assistant_id}",
    response_model=OutboundCallResponse,
    status_code=status.HTTP_200_OK,
)
@limiter.limit("30/minute")
async def make_outbound_call(
    request: Request,
    assistant_id: str,
    body: OutboundCallRequest,
    current_user: dict = Depends(get_current_user),
):
    """Place an outbound PSTN call to `body.phone_number`.

    Auth: JWT required. Caller must own the assistant. Rate-limited.
    Atomicity: call_log row inserted BEFORE Twilio API call so a partial
    failure (network drop after Twilio dispatch) is recoverable.
    """
    phone_number = body.phone_number.strip()
    if not E164.match(phone_number):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid phone number format. Use E.164 format (e.g., +1234567890).",
        )

    db = Database.get_db()
    try:
        assistant_obj_id = ObjectId(assistant_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assistant_id format"
        )

    caller_user_id = current_user["user_id"]
    try:
        caller_user_obj_id = ObjectId(caller_user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user")

    # Caller must own the assistant. Returns 404 (not 403) to avoid leaking
    # existence of other users' assistants via probe.
    assistant = db["assistants"].find_one(
        {"_id": assistant_obj_id, "user_id": caller_user_obj_id}
    )
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Assistant not found"
        )

    # Pick the FROM number. If the caller specified a particular number
    # (frontend "Phone" button on a specific card), honor that — the user
    # picked it because they want that provider/number for this call. Without
    # this, an assistant with both a Twilio and a Vobiz number ends up using
    # whichever Mongo returns first, ignoring the user's selection.
    phone_number_doc = None
    if body.from_phone_number_id:
        try:
            from_phone_obj_id = ObjectId(body.from_phone_number_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid from_phone_number_id format",
            )
        phone_number_doc = db["phone_numbers"].find_one({
            "_id": from_phone_obj_id,
            "user_id": caller_user_obj_id,
            "status": "active",
        })
        if not phone_number_doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="from_phone_number_id not found or not owned by caller",
            )
        # The picked number MUST be assigned to this assistant. Without this
        # check, a caller could dial out from number B (Vobiz) under assistant
        # A's identity (Twilio) — caller-ID + greeting mismatch, billing/
        # analytics attribution corruption, and a compliance issue.
        if phone_number_doc.get("assigned_assistant_id") != assistant_obj_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="from_phone_number_id is not assigned to this assistant. "
                       "Reassign the number first, then place the call.",
            )
    else:
        # Legacy fallback: any number currently assigned to the assistant.
        phone_number_doc = db["phone_numbers"].find_one({
            "assigned_assistant_id": assistant_obj_id,
            "user_id": caller_user_obj_id,
            "status": "active",
        })

    if not phone_number_doc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No phone number assigned to this assistant",
        )
    caller_id = phone_number_doc["phone_number"]

    try:
        config = load_assistant_config(assistant_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    room_name = generate_room_name(prefix="pstn-out")
    transport = _outbound_transport_for(phone_number_doc)

    # Insert log FIRST so a partial failure (network blip mid-dial) leaves
    # a recoverable row. We update with twilio_call_sid after dial succeeds.
    now = datetime.now(timezone.utc)
    initial_log = {
        "user_id": caller_user_obj_id,
        "assistant_id": assistant_obj_id,
        "assistant_name": assistant.get("name"),
        "phone_number": phone_number_doc["_id"],
        "phone_number_value": caller_id,
        "call_sid": room_name,  # provisional; replaced with twilio_call_sid for TwiML
        "twilio_call_sid": None,
        "livekit_room": room_name,
        "direction": "outbound",
        "from_number": caller_id,
        "to_number": phone_number,
        "status": "initiating",
        "voice_config": {
            "transport": transport,
            "asr_provider": "deepgram",
            "asr_model": config.get("asr_model"),
            "tts_provider": "elevenlabs",
            "tts_model": config.get("tts_model"),
            "tts_voice": config.get("tts_voice"),
            "llm_provider": "openai",
            "llm_model": config.get("llm_model"),
        },
        "created_at": now,
        "updated_at": now,
    }
    log_insert = db["call_logs"].insert_one(initial_log)
    log_id = log_insert.inserted_id

    twilio_call_sid: str | None = None
    try:
        await create_room_with_agent(
            room_name=room_name,
            assistant_config=config,
            metadata_extra={
                "source": "pstn",
                "direction": "outbound",
                "to_number": phone_number,
                "from_number": caller_id,
            },
        )

        if transport == "livekit-sip":
            # Per-number trunk override (e.g. Vobiz number → vobiz outbound trunk).
            # Falls back to global env in dial_outbound_sip if not set on doc.
            trunk_id = phone_number_doc.get("livekit_outbound_trunk_id")
            await dial_outbound_sip(
                room_name=room_name,
                phone_number=phone_number,
                caller_id=caller_id,
                participant_identity="pstn-callee",
                participant_name=phone_number,
                trunk_id=trunk_id,
            )
        else:
            status_cb = None
            recording_cb = None
            base = settings.api_base_url or settings.base_url
            if base:
                base = base.rstrip('/')
                status_cb = f"{base}/webhooks/twilio/calls"
                # Recording webhook lives under /api/twilio-webhooks/recording.
                # When set, Twilio records both legs of the bridge and posts the
                # mp3 URL here when finalized; webhook updates call_logs +
                # triggers transcription.
                recording_cb = f"{base}/api/twilio-webhooks/recording"
            twilio_call_sid = await dial_outbound_via_twilio(
                room_name=room_name,
                phone_number=phone_number,
                caller_id=caller_id,
                status_callback_url=status_cb,
                recording_callback_url=recording_cb,
            )
    except (LiveKitNotConfigured, TwilioNotConfigured) as exc:
        db["call_logs"].update_one(
            {"_id": log_id},
            {"$set": {"status": "failed", "failure_reason": str(exc), "updated_at": datetime.now(timezone.utc)}},
        )
        # Best-effort tear down the room we created
        try:
            await hangup_room(room_name)
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except Exception as exc:
        logger.error("Outbound dial failed (transport=%s)", transport, exc_info=True)
        db["call_logs"].update_one(
            {"_id": log_id},
            {"$set": {"status": "failed", "failure_reason": str(exc)[:500], "updated_at": datetime.now(timezone.utc)}},
        )
        try:
            await hangup_room(room_name)
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to place outbound call: {exc}",
        )

    # Success path: update log with the call_sid we want callers to use.
    external_call_sid = twilio_call_sid or room_name
    db["call_logs"].update_one(
        {"_id": log_id},
        {"$set": {
            "call_sid": external_call_sid,
            "twilio_call_sid": twilio_call_sid,
            "status": "initiated",
            "updated_at": datetime.now(timezone.utc),
        }},
    )

    return OutboundCallResponse(
        message=f"Outbound call initiated via {transport}",
        call_sid=external_call_sid,
        status="initiated",
        assistant_id=assistant_id,
    )


@router.post("/hangup/{call_sid}/{user_id}", status_code=status.HTTP_200_OK)
@router.post("/hangup/{call_sid}", status_code=status.HTTP_200_OK)
@limiter.limit("60/minute")
async def hangup_call(
    request: Request,
    call_sid: str,
    user_id: str | None = None,  # legacy frontend path; ignored, JWT is authoritative
    current_user: dict = Depends(get_current_user),
):
    """Hang up an active call. Caller must own the call_log.

    Legacy `/hangup/{call_sid}/{user_id}` is accepted for frontend compatibility,
    but the user_id is ignored — only the JWT subject is trusted.
    """
    db = Database.get_db()
    try:
        caller_user_obj_id = ObjectId(current_user["user_id"])
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user")

    call_log = db["call_logs"].find_one(
        {"call_sid": call_sid, "user_id": caller_user_obj_id}
    )
    if not call_log:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Call not found"
        )

    room_name = call_log.get("livekit_room") or call_sid
    try:
        await hangup_room(room_name)
    except LiveKitNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )
    except Exception as exc:
        logger.error("LiveKit room hangup failed for %s: %s", call_sid, exc)

    twilio_sid = call_log.get("twilio_call_sid")
    if twilio_sid:
        try:
            await hangup_twilio_call(twilio_sid)
        except TwilioNotConfigured as exc:
            logger.warning("Twilio not configured during hangup: %s", exc)
        except Exception as exc:
            logger.warning("Twilio hangup failed for %s: %s", twilio_sid, exc)

    db["call_logs"].update_one(
        {"_id": call_log["_id"]},
        {"$set": {"status": "completed", "updated_at": datetime.now(timezone.utc)}},
    )
    return {"message": "Call ended", "call_sid": call_sid, "status": "completed"}


@router.get("/call-status/{call_sid}/{user_id}", status_code=status.HTTP_200_OK)
@router.get("/call-status/{call_sid}", status_code=status.HTTP_200_OK)
@limiter.limit("100/minute")
async def get_call_status(
    request: Request,
    call_sid: str,
    user_id: str | None = None,  # legacy compat; ignored
    current_user: dict = Depends(get_current_user),
):
    """Return the stored status of a call. Caller must own it."""
    try:
        caller_user_obj_id = ObjectId(current_user["user_id"])
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid user")

    call_log = Database.get_db()["call_logs"].find_one(
        {"call_sid": call_sid, "user_id": caller_user_obj_id}
    )
    if not call_log:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Call not found"
        )

    return {
        "call_sid": call_sid,
        "livekit_room": call_log.get("livekit_room"),
        "status": call_log.get("status"),
        "direction": call_log.get("direction"),
        "from": call_log.get("from_number"),
        "to": call_log.get("to_number"),
        "duration": call_log.get("duration"),
        "start_time": call_log.get("created_at").isoformat() if call_log.get("created_at") else None,
        "end_time": call_log.get("updated_at").isoformat() if call_log.get("updated_at") else None,
    }


@router.post(
    "/check-number",
    response_model=CheckNumberResponse,
    status_code=status.HTTP_200_OK,
)
async def check_phone_number(
    phone_number: str,
    current_user: dict = Depends(get_current_user),
):
    """E.164 validation only."""
    phone_number = phone_number.strip()
    is_valid = bool(E164.match(phone_number))
    return CheckNumberResponse(
        phone_number=phone_number,
        is_allowed=is_valid,
        message=("Number is valid E.164" if is_valid else "Invalid E.164 format"),
    )
