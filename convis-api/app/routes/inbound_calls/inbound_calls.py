"""Inbound call routing — Twilio PSTN hand-off to LiveKit SIP.

When Twilio receives an inbound call to a number assigned to an assistant, it
POSTs this service's webhook. We:
    1. Look up the assistant.
    2. Create a LiveKit room and dispatch the agent into it.
    3. Return TwiML that forwards Twilio's media to LiveKit SIP ingress — the
       caller lands in the same room as the agent.

No audio flows through this API; LiveKit is the media plane.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from twilio.twiml.voice_response import Dial, Sip, VoiceResponse

from app.config.database import Database
from app.config.settings import settings
from app.models.inbound_calls import InboundCallConfig, InboundCallResponse
from app.services.livekit.assistant_config import load_assistant_config
from app.services.livekit.sip_service import create_room_with_agent, generate_room_name
from app.services.livekit.tokens import LiveKitNotConfigured
from app.utils.auth import get_current_user
from app.utils.twilio_signature import verify_twilio_signature

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_class=JSONResponse)
async def inbound_calls_index():
    return {"message": "Inbound calls service is running"}


def _livekit_sip_uri(room_name: str) -> str:
    if not settings.livekit_sip_inbound_host:
        raise LiveKitNotConfigured(
            "LIVEKIT_SIP_INBOUND_HOST must be set to the LiveKit Cloud SIP URI host"
        )
    # Room is selected via the `room` user-part; LiveKit SIP ingress reads it.
    return f"sip:{room_name}@{settings.livekit_sip_inbound_host}"


async def _provision_call(
    assistant_id: str,
    *,
    direction: str,
    from_number: Optional[str] = None,
    call_sid: Optional[str] = None,
    resumed_after_failed_transfer: bool = False,
) -> str:
    """Load assistant, create a LiveKit room with agent dispatched, return room name.

    `call_sid` (when supplied) is threaded into room metadata so the agent's
    transfer_to_agent tool can redirect the live Twilio call. `resumed_after_failed_transfer`
    tells the agent to open with an apology instead of the normal greeting.
    """
    try:
        config = load_assistant_config(assistant_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    room_name = generate_room_name(prefix="pstn-in")
    metadata_extra: dict = {
        "source": "pstn",
        "direction": direction,
        "from_number": from_number,
    }
    if call_sid:
        metadata_extra["call_sid"] = call_sid
    if resumed_after_failed_transfer:
        metadata_extra["resumed_after_failed_transfer"] = True
    await create_room_with_agent(
        room_name=room_name,
        assistant_config=config,
        metadata_extra=metadata_extra,
    )
    return room_name


@router.api_route(
    "/connect/{assistant_id}",
    methods=["GET", "POST"],
    dependencies=[Depends(verify_twilio_signature)],
)
async def twilio_inbound_webhook(assistant_id: str, request: Request):
    """Twilio Voice webhook for inbound calls. Returns TwiML that SIP-dials
    the caller into a LiveKit room where the agent is already waiting."""
    form = await request.form() if request.method == "POST" else request.query_params
    call_sid = form.get("CallSid")
    from_number = form.get("From")
    to_number = form.get("To")

    logger.info(
        "[INBOUND] Twilio webhook: CallSid=%s From=%s To=%s assistant=%s",
        call_sid, from_number, to_number, assistant_id,
    )

    # Validate config up front — avoid creating a LiveKit room we can't dial into.
    if not settings.livekit_sip_inbound_host:
        logger.error("LIVEKIT_SIP_INBOUND_HOST is not configured — refusing call")
        response = VoiceResponse()
        response.say("Service is temporarily unavailable. Please try again later.")
        return PlainTextResponse(str(response), media_type="application/xml")

    try:
        room_name = await _provision_call(
            assistant_id, direction="inbound", from_number=from_number, call_sid=call_sid
        )
    except LiveKitNotConfigured as exc:
        logger.error("LiveKit not configured: %s", exc)
        response = VoiceResponse()
        response.say("Service is temporarily unavailable. Please try again later.")
        return PlainTextResponse(str(response), media_type="application/xml")

    # Log the inbound call. Stamp user_id + assistant_name from the owning
    # assistant so the dashboard's per-user call list (filters by user_id)
    # shows inbound calls and the assistant column resolves correctly —
    # without this, inbound rows are invisible to their owner and the UI
    # renders "Unknown Assistant".
    try:
        db = Database.get_db()
        owning_assistant = db["assistants"].find_one(
            {"_id": ObjectId(assistant_id)}, {"user_id": 1, "name": 1}
        )
        owner_user_id = owning_assistant.get("user_id") if owning_assistant else None
        owner_assistant_name = owning_assistant.get("name") if owning_assistant else None
        db["call_logs"].insert_one({
            "call_sid": call_sid,
            "assistant_id": ObjectId(assistant_id),
            "assistant_name": owner_assistant_name,
            "user_id": owner_user_id,
            "direction": "inbound",
            "from_number": from_number,
            "to_number": to_number,
            "status": "ringing",
            "livekit_room": room_name,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        })
    except Exception as exc:
        logger.warning("Failed to log inbound call: %s", exc)

    # Tell Twilio to record the bridged call. Without `record=...` on <Dial>,
    # Twilio captures no audio for inbound legs — outbound calls record because
    # they're initiated via REST with a recording_callback_url. We *always*
    # request recording so audio is captured even if the callback URL is
    # misconfigured (fail-loud: a missed mp3 is recoverable from Twilio Console;
    # a missed recording is gone forever). The webhook
    # (/api/twilio-webhooks/recording) updates call_logs with the mp3 URL and
    # triggers transcription / sentiment analysis when reachable.
    base = settings.api_base_url or settings.base_url
    record_cb = f"{base.rstrip('/')}/api/twilio-webhooks/recording" if base else None
    if not record_cb:
        logger.warning(
            "[INBOUND] api_base_url/base_url not set — recording will still be "
            "captured by Twilio but callback won't fire (no auto-transcription). "
            "Recording can be retrieved from Twilio Console for CallSid=%s",
            call_sid,
        )

    response = VoiceResponse()
    dial_kwargs = {
        "answer_on_bridge": True,
        # Dual-channel: caller and callee on separate stereo tracks. Required
        # for accurate speaker-attributed transcription downstream.
        "record": "record-from-answer-dual",
    }
    if record_cb:
        dial_kwargs.update({
            "recording_status_callback": record_cb,
            "recording_status_callback_method": "POST",
            # Subscribe to both completion AND failure so we learn about
            # silent-record failures instead of just assuming success.
            "recording_status_callback_event": "completed failed",
        })
    dial = Dial(**dial_kwargs)
    dial.sip(_livekit_sip_uri(room_name))
    response.append(dial)

    logger.info("[INBOUND] Routing call %s to LiveKit room %s", call_sid, room_name)
    return PlainTextResponse(str(response), media_type="application/xml")


# Backwards-compatible alias (existing Twilio webhooks may point here)
@router.api_route(
    "/incoming-call/{assistant_id}",
    methods=["GET", "POST"],
    dependencies=[Depends(verify_twilio_signature)],
)
async def incoming_call_alias(assistant_id: str, request: Request):
    return await twilio_inbound_webhook(assistant_id, request)


@router.get(
    "/config/{assistant_id}",
    response_model=InboundCallResponse,
    status_code=status.HTTP_200_OK,
)
async def get_inbound_call_config(
    assistant_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Return an assistant's voice config to its owner.

    Auth + ownership enforced. system_message routinely contains business
    secrets (price lists, internal scripts, integration tokens), so we treat
    it the same as any other tenant-scoped resource. We deliberately return
    404 (not 403) when the caller doesn't own the assistant — otherwise the
    response would reveal whether a given assistant_id exists.
    """
    try:
        assistant_obj_id = ObjectId(assistant_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid assistant_id format"
        )

    assistant = Database.get_db()["assistants"].find_one({"_id": assistant_obj_id})
    if not assistant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="AI assistant not found"
        )

    # Probe-prevention: same 404 whether the assistant doesn't exist or
    # belongs to another tenant. Admins (per JWT role claim) may read any.
    is_owner = str(assistant.get("user_id")) == current_user["user_id"]
    is_admin = current_user.get("token_role") == "admin"
    if not (is_owner or is_admin):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="AI assistant not found"
        )

    return InboundCallResponse(
        message="Configuration retrieved successfully",
        config=InboundCallConfig(
            assistant_id=str(assistant["_id"]),
            system_message=assistant["system_message"],
            voice=assistant["voice"],
            temperature=assistant["temperature"],
        ),
    )


@router.api_route(
    "/recording-status",
    methods=["GET", "POST"],
    dependencies=[Depends(verify_twilio_signature)],
)
async def handle_recording_status(request: Request):
    """Twilio recording-status webhook. Saves recording URL and kicks off
    post-call processing (appointment booking, transcription).

    Twilio signature verification is enforced — without it, an unauthenticated
    attacker could poison call_logs with arbitrary recording URLs and trigger
    SSRF via the post-call processor.
    """
    # `verify_twilio_signature` already consumed the form body; re-reading
    # request.form() returns Starlette's cached parse.
    form = await request.form() if request.method == "POST" else request.query_params

    recording_status = form.get("RecordingStatus")
    call_sid = form.get("CallSid")
    if recording_status != "completed" or not call_sid:
        return {"status": "success", "message": "Recording status received"}

    db = Database.get_db()
    update = {
        "recording_sid": form.get("RecordingSid"),
        "recording_url": form.get("RecordingUrl"),
        "recording_duration": int(form.get("RecordingDuration") or 0) or None,
        "recording_status": recording_status,
        "updated_at": datetime.utcnow(),
    }
    # Scope the update to inbound rows only — this webhook is mounted under
    # /api/inbound-calls/, so applying it to outbound rows would let a
    # crafted (but signature-valid) callback for one call clobber the
    # recording on a different call's row.
    result = db["call_logs"].update_one(
        {"call_sid": call_sid, "direction": "inbound"},
        {"$set": update},
    )
    if result.modified_count == 0:
        logger.warning("Inbound call log not found for call_sid %s", call_sid)
        return {"status": "success", "message": "Recording status received"}

    call_log = db["call_logs"].find_one({"call_sid": call_sid, "direction": "inbound"})
    if call_log and update["recording_url"] and call_log.get("assistant_id"):
        try:
            from app.services.async_inbound_post_call_processor import AsyncInboundPostCallProcessor

            asyncio.create_task(
                AsyncInboundPostCallProcessor().process_inbound_call(
                    call_sid=call_sid,
                    assistant_id=str(call_log["assistant_id"]),
                    recording_url=update["recording_url"],
                )
            )
        except Exception as exc:
            logger.error("Failed to trigger post-call processing: %s", exc)

    return {"status": "success", "message": "Recording status received"}


@router.api_route(
    "/transcription-status",
    methods=["GET", "POST"],
    dependencies=[Depends(verify_twilio_signature)],
)
async def handle_transcription_status(request: Request):
    """Twilio transcription-status webhook. Twilio signature verification is
    enforced — without it, an attacker could inject arbitrary text into
    `call_log.transcription_text`, which surfaces in the dashboard transcript
    view (stored XSS) and the post-call sentiment / summary pipeline.
    """
    form = await request.form() if request.method == "POST" else request.query_params

    transcription_status = form.get("TranscriptionStatus")
    call_sid = form.get("CallSid")
    transcription_text = form.get("TranscriptionText")
    if transcription_status != "completed" or not (call_sid and transcription_text):
        return {"status": "success", "message": "Transcription status received"}

    # Scope to inbound only — this webhook is mounted under /api/inbound-calls/.
    Database.get_db()["call_logs"].update_one(
        {"call_sid": call_sid, "direction": "inbound"},
        {
            "$set": {
                "transcription_sid": form.get("TranscriptionSid"),
                "transcription_text": transcription_text,
                "transcription_url": form.get("TranscriptionUrl"),
                "transcription_status": transcription_status,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    return {"status": "success", "message": "Transcription status received"}
