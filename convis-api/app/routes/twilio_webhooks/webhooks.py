"""
Dynamic Twilio Webhook Router
Routes voice calls and SMS based on the To number (or AccountSid for subaccounts)
No manual webhook configuration needed - one endpoint handles all numbers.
"""

from fastapi import APIRouter, Depends, Request, Form, HTTPException, status
from fastapi.responses import HTMLResponse
from typing import Optional
from bson import ObjectId
import logging

from app.config.database import Database
from app.config.async_database import AsyncDatabase
from app.config.settings import settings
from app.services.async_call_status_processor import process_call_status_async
from app.utils.twilio_signature import verify_twilio_signature
from twilio.twiml.voice_response import VoiceResponse, Connect
from twilio.twiml.messaging_response import MessagingResponse

logger = logging.getLogger(__name__)

# Every route on this router carries a Twilio signature check. New endpoints
# added here are protected by default — opt-out only with explicit
# `dependencies=[]` and a written justification.
router = APIRouter(dependencies=[Depends(verify_twilio_signature)])


# ==================== Helper Functions ====================

async def _trigger_summary_extraction_for_call_sid(call_sid: str) -> None:
    """Look up the call_log for `call_sid` and, if its transcription is
    present, fire the conversation-memory summary extraction.

    Idempotent via the unique index on `call_summaries.call_log_id`.
    Gated INSIDE `extract_and_persist_summary` on
    `assistants.conversation_history_enabled`, so assistants without
    the feature pay zero LLM cost.

    Used by BOTH transcription paths:
      • the live `recording` webhook flow (line ~816 below)
      • the legacy `_trigger_transcription_after_delay` helper
    """
    import asyncio
    try:
        def _fetch():
            # Resolve the collection HERE, inside the helper — `call_logs_collection`
            # in `recording_callback` is a local variable scoped to that handler.
            from app.config.database import Database
            # Both field names exist in the codebase: `transcript` (the live
            # path in async_post_call_processor.transcribe_and_update_call
            # writes this) and `transcription` (some legacy paths write this).
            # Project BOTH so we can fall back if one is empty.
            return Database.get_db()["call_logs"].find_one(
                {"call_sid": call_sid},
                {"_id": 1, "transcript": 1, "transcription": 1},
            )
        # pymongo is sync — must run off-thread to avoid blocking the loop.
        call_log = await asyncio.to_thread(_fetch)
        # Accept either field name; the live transcription path writes
        # `transcript` (singular) which is what we'll see in practice.
        transcript_text = (
            (call_log.get("transcript") if call_log else None)
            or (call_log.get("transcription") if call_log else None)
            or ""
        ).strip()
        if call_log and transcript_text:
            from app.services.post_call_summary_service import extract_and_persist_summary
            asyncio.create_task(extract_and_persist_summary(str(call_log["_id"])))
            logger.info(
                "[POST_CALL] scheduled summary extraction for call_log=%s "
                "(call_sid=%s)", call_log["_id"], call_sid,
            )
        else:
            logger.info(
                "[POST_CALL] no transcription on call_log for call_sid=%s — "
                "skipping summary extraction (will be retried by backfill "
                "loop if transcript lands later)", call_sid,
            )
    except Exception:
        logger.exception(
            "[POST_CALL] failed to schedule summary extraction for %s "
            "(this is non-fatal; backfill loop will retry)", call_sid,
        )


async def _transcribe_and_summarize(processor, call_sid: str, recording_url: str) -> None:
    """Chained task: await transcription, then trigger summary extraction.

    Replaces the prior fire-and-forget `asyncio.create_task(
    processor.transcribe_and_update_call(...))` at the recording-webhook
    site — we need the transcription to FINISH (and write to
    call_logs.transcription) BEFORE we can extract a summary from it.

    Both steps tolerate failure in the other:
      • If transcription fails, the summary step is skipped (no point
        extracting from nothing).
      • If summary extraction fails, the transcription is still saved.
    """
    try:
        await processor.transcribe_and_update_call(call_sid, recording_url)
    except Exception:
        logger.exception(
            "[POST_CALL] transcription failed for call_sid=%s — "
            "skipping summary extraction", call_sid,
        )
        return
    await _trigger_summary_extraction_for_call_sid(call_sid)


async def _trigger_transcription_after_delay(call_sid: str, delay_seconds: int = 5):
    """
    Wait for recording to be ready, then trigger transcription.
    OPTIMIZED: Uses async MongoDB operations.

    Args:
        call_sid: Twilio Call SID
        delay_seconds: Seconds to wait before checking
    """
    import asyncio
    await asyncio.sleep(delay_seconds)

    try:
        db = await AsyncDatabase.get_db()
        call_logs_collection = db['call_logs']

        # Check if recording URL exists (async)
        call_log = await call_logs_collection.find_one({"call_sid": call_sid})
        if not call_log:
            logger.warning(f"Call log not found for transcription: {call_sid}")
            return

        recording_url = call_log.get("recording_url")
        if not recording_url:
            logger.info(f"No recording URL yet for {call_sid}, will transcribe when recording callback arrives")
            return

        # Trigger transcription using async processor
        from app.services.async_post_call_processor import AsyncPostCallProcessor
        processor = AsyncPostCallProcessor()

        logger.info(f"Starting transcription for call: {call_sid}")
        await processor.transcribe_and_update_call(call_sid, recording_url)

        # Chain post-call summary extraction (conversation-memory feature).
        # Same helper used by the live recording-webhook path so both
        # transcription chains feed the same summary pipeline.
        await _trigger_summary_extraction_for_call_sid(call_sid)

    except Exception as e:
        logger.error(f"Error triggering transcription for {call_sid}: {e}")


# ==================== Voice Webhook ====================

@router.api_route("/voice", methods=["GET", "POST"])
async def voice_webhook(
    request: Request,
    To: Optional[str] = Form(None),
    From: Optional[str] = Form(None),
    CallSid: Optional[str] = Form(None),
    AccountSid: Optional[str] = Form(None)
):
    """
    Dynamic voice webhook router — routes ALL inbound voice calls based on
    the dialed number's `assigned_assistant_id`.

    This is the single Twilio webhook URL set on the platform's TwiML
    Application (numbers purchased through the dashboard's "Purchase Number"
    flow point their `voice_application_sid` here, so a single endpoint
    handles every number on the account).

    Implementation: provisions a LiveKit room with the assigned assistant +
    returns TwiML that SIP-bridges Twilio's audio into that room — symmetric
    with the explicit per-assistant webhook at
    `/api/inbound-calls/connect/{assistant_id}`. Both paths converge at LiveKit.

    Pre-LiveKit: this endpoint emitted `<Stream>` to a custom WebSocket
    pipeline. That pipeline was deleted in the LiveKit migration but this
    endpoint kept emitting the dead URL — so any number routed via the
    TwiML App silently broke at handshake time.
    """
    # Lazy imports — these pull livekit-agents which is heavy; only needed
    # on actual inbound calls, not on every module load.
    from app.routes.inbound_calls.inbound_calls import _provision_call, _livekit_sip_uri
    from app.services.livekit.tokens import LiveKitNotConfigured
    from twilio.twiml.voice_response import Dial
    from datetime import datetime

    try:
        logger.info(f"Voice webhook - To: {To}, From: {From}, CallSid: {CallSid}")

        if not To:
            # GET fallback — Twilio sometimes uses GET for retries.
            To = request.query_params.get('To')
            From = request.query_params.get('From')
            CallSid = request.query_params.get('CallSid')
            AccountSid = request.query_params.get('AccountSid')

        if not To:
            logger.error("No 'To' parameter in webhook request")
            response = VoiceResponse()
            response.say("Sorry, we could not process your call. Please try again later.")
            return HTMLResponse(content=str(response), media_type="application/xml")

        db = Database.get_db()
        phone_numbers_collection = db['phone_numbers']
        assistants_collection = db['assistants']

        # Look up the phone number in our database
        phone_doc = phone_numbers_collection.find_one({"phone_number": To})
        if not phone_doc:
            logger.warning(f"Phone number {To} not found in database")
            response = VoiceResponse()
            response.say("Sorry, this number is not configured. Please contact support.")
            return HTMLResponse(content=str(response), media_type="application/xml")

        if not phone_doc.get("assigned_assistant_id"):
            logger.warning(f"No assistant assigned to {To}")
            response = VoiceResponse()
            response.say("Sorry, this number is not yet configured with an AI assistant. Please contact support.")
            return HTMLResponse(content=str(response), media_type="application/xml")

        assistant_id = str(phone_doc["assigned_assistant_id"])
        assistant = assistants_collection.find_one({"_id": ObjectId(assistant_id)})
        if not assistant:
            logger.error(f"Assistant {assistant_id} not found for number {To}")
            response = VoiceResponse()
            response.say("Sorry, configuration error. Please contact support.")
            return HTMLResponse(content=str(response), media_type="application/xml")

        # Validate LiveKit config — fail loud instead of silently dropping the call.
        if not settings.livekit_sip_inbound_host:
            logger.error("LIVEKIT_SIP_INBOUND_HOST is not configured — refusing call")
            response = VoiceResponse()
            response.say("Service is temporarily unavailable. Please try again later.")
            return HTMLResponse(content=str(response), media_type="application/xml")

        # Provision a LiveKit room with the agent dispatched and ready.
        # Pass CallSid so the agent's transfer_to_agent tool can redirect this
        # live Twilio call to a human if the assistant has call transfer on.
        try:
            room_name = await _provision_call(
                assistant_id, direction="inbound", from_number=From, call_sid=CallSid,
            )
        except LiveKitNotConfigured as exc:
            logger.error(f"LiveKit not configured: {exc}")
            response = VoiceResponse()
            response.say("Service is temporarily unavailable. Please try again later.")
            return HTMLResponse(content=str(response), media_type="application/xml")

        # Stamp call_log so the dashboard shows this call (mirror of the
        # inbound_calls.py behaviour shipped in the security-fix audit).
        try:
            db["call_logs"].insert_one({
                "call_sid": CallSid,
                "assistant_id": ObjectId(assistant_id),
                "assistant_name": assistant.get("name"),
                "user_id": phone_doc.get("user_id"),
                "direction": "inbound",
                "from_number": From,
                "to_number": To,
                "status": "ringing",
                "livekit_room": room_name,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            })
        except Exception as exc:
            logger.warning(f"Failed to log inbound call: {exc}")

        # Recording webhook — same pattern as inbound_calls.py.
        base = settings.api_base_url or settings.base_url
        record_cb = f"{base.rstrip('/')}/api/twilio-webhooks/recording" if base else None
        if not record_cb:
            logger.warning(
                f"[VOICE] api_base_url unset — recording will still capture but "
                f"callback won't fire. CallSid={CallSid}"
            )

        response = VoiceResponse()
        dial_kwargs = {
            "answer_on_bridge": True,
            "record": "record-from-answer-dual",
        }
        if record_cb:
            dial_kwargs.update({
                "recording_status_callback": record_cb,
                "recording_status_callback_method": "POST",
                "recording_status_callback_event": "completed failed",
            })
        dial = Dial(**dial_kwargs)
        dial.sip(_livekit_sip_uri(room_name))
        response.append(dial)

        logger.info(f"[VOICE] Routing call {CallSid} → assistant={assistant_id} → LiveKit room {room_name}")
        return HTMLResponse(content=str(response), media_type="application/xml")

    except Exception as error:
        import traceback
        logger.error(f"Error in voice webhook: {str(error)}")
        logger.error(traceback.format_exc())

        # Return error TwiML
        response = VoiceResponse()
        response.say("Sorry, an error occurred. Please try again later.")
        return HTMLResponse(content=str(response), media_type="application/xml")


@router.api_route("/transfer-result", methods=["GET", "POST"])
async def transfer_result_callback(
    request: Request,
    CallSid: Optional[str] = Form(None),
    DialCallStatus: Optional[str] = Form(None),
    dir: str = "inbound",
):
    """Twilio `<Dial action>` callback fired after a call-transfer `<Dial>` ends.

    Set by `sip_service.transfer_twilio_call_to_number`. If the human answered
    and the call ended normally → just hang up. If they didn't answer / were
    busy / failed → re-provision a fresh LiveKit room for the same assistant
    (with `resumed_after_failed_transfer=True`) and re-bridge the caller to the
    AI so the call isn't dropped on the floor.
    """
    from datetime import datetime, timezone

    # GET fallback (Twilio occasionally retries via GET).
    if not CallSid:
        CallSid = request.query_params.get("CallSid")
    if not DialCallStatus:
        DialCallStatus = request.query_params.get("DialCallStatus")
    dir = request.query_params.get("dir") or dir or "inbound"

    logger.info("[TRANSFER_RESULT] CallSid=%s DialCallStatus=%s dir=%s", CallSid, DialCallStatus, dir)

    # Human answered & the bridged call ended normally — hang up the parent leg.
    if (DialCallStatus or "").lower() == "completed":
        if CallSid:
            try:
                Database.get_db()["call_logs"].update_many(
                    {"call_sid": CallSid},
                    {"$set": {"transferred": True, "transfer_outcome": "answered",
                              "updated_at": datetime.now(timezone.utc)}},
                )
            except Exception:
                logger.debug("[TRANSFER_RESULT] completed-stamp failed", exc_info=True)
        r = VoiceResponse()
        r.hangup()
        return HTMLResponse(content=str(r), media_type="application/xml")

    # No-answer / busy / failed / canceled → hand the caller back to the AI.
    try:
        from app.routes.inbound_calls.inbound_calls import _provision_call, _livekit_sip_uri
        from app.services.livekit.tokens import LiveKitNotConfigured
        from twilio.twiml.voice_response import Dial

        db = Database.get_db()
        call_log = db["call_logs"].find_one({"call_sid": CallSid}) if CallSid else None
        assistant_id = call_log.get("assistant_id") if call_log else None
        direction = (call_log.get("direction") if call_log else None) or dir or "inbound"
        if not assistant_id or not settings.livekit_sip_inbound_host:
            r = VoiceResponse()
            r.say("Sorry, we couldn't connect your call. Please try again later.")
            r.hangup()
            return HTMLResponse(content=str(r), media_type="application/xml")

        room_name = await _provision_call(
            str(assistant_id),
            direction=direction,
            from_number=(call_log.get("from_number") if call_log else None),
            call_sid=CallSid,
            resumed_after_failed_transfer=True,
        )
        # Re-point livekit_room so _mark_call_completed finds the call_log when
        # the resumed leg ends, and record the failed-transfer outcome.
        try:
            db["call_logs"].update_many(
                {"call_sid": CallSid},
                {"$set": {
                    "transferred": False,  # handoff did NOT complete (human never picked up)
                    "transfer_failed": True,
                    "transfer_failure_status": DialCallStatus,
                    "transfer_outcome": (DialCallStatus or "no-answer"),
                    "livekit_room": room_name,
                    "status": "in-progress",
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
        except Exception:
            logger.debug("[TRANSFER_RESULT] call_log re-point failed", exc_info=True)

        r = VoiceResponse()
        dial = Dial(answer_on_bridge=True)
        dial.sip(_livekit_sip_uri(room_name))
        r.append(dial)
        logger.info("[TRANSFER_RESULT] re-bridged call %s → assistant=%s → room %s", CallSid, assistant_id, room_name)
        return HTMLResponse(content=str(r), media_type="application/xml")
    except Exception:
        import traceback
        logger.error("[TRANSFER_RESULT] failed: %s", traceback.format_exc())
        r = VoiceResponse()
        r.say("Sorry, we couldn't connect your call.")
        r.hangup()
        return HTMLResponse(content=str(r), media_type="application/xml")


@router.api_route("/voice-status", methods=["GET", "POST"])
async def voice_status_callback(
    request: Request,
    CallSid: Optional[str] = Form(None),
    CallStatus: Optional[str] = Form(None),
    To: Optional[str] = Form(None),
    From: Optional[str] = Form(None),
    CallDuration: Optional[str] = Form(None)
):
    """
    Voice status callback - receives call status updates.

    Twilio sends status updates as calls progress:
    - initiated, ringing, in-progress, completed, busy, failed, no-answer

    You can use this to log call analytics, update dashboards, etc.

    Args:
        CallSid: Call SID
        CallStatus: Current status
        To: Twilio number
        From: Caller number
        CallDuration: Duration in seconds

    Returns:
        dict: Success message
    """
    try:
        logger.info(f"Voice status - CallSid: {CallSid}, Status: {CallStatus}, Duration: {CallDuration}s")

        # You can add custom logic here:
        # - Log to analytics database
        # - Update real-time dashboard
        # - Send notifications
        # - Calculate costs

        # For now, just log it
        db = Database.get_db()
        call_logs_collection = db['call_logs']

        if CallSid:
            # Update-only: do NOT upsert. The inbound and outbound creation
            # paths are responsible for the initial call_log insert with all
            # required fields (user_id, assistant_id, direction, etc.).
            # An upsert here would create orphan rows missing those fields —
            # invisible to per-user dashboards and unable to attribute the
            # call to anyone. Production previously accumulated 1,619 such
            # orphans from this exact path.
            update_result = call_logs_collection.update_one(
                {"call_sid": CallSid},
                {
                    "$set": {
                        "status": CallStatus,
                        "duration": int(CallDuration) if CallDuration else None,
                        "updated_at": datetime.utcnow()
                    }
                },
            )
            if update_result.matched_count == 0:
                logger.warning(
                    "voice-status webhook for unknown CallSid=%s — ignoring "
                    "(no matching call_log; the inbound/outbound webhook should "
                    "have already created the row).",
                    CallSid,
                )

            # Trigger transcription and cost calculation when call completes
            if CallStatus == "completed":
                logger.info(f"Call completed, checking for recording to transcribe: {CallSid}")

                # Wait a few seconds for recording to be ready
                import asyncio
                asyncio.create_task(_trigger_transcription_after_delay(CallSid, 5))

                # Calculate and store call cost
                try:
                    from app.services.cost_calculator import calculate_and_store_call_cost
                    duration_seconds = int(CallDuration) if CallDuration else 0
                    if duration_seconds > 0:
                        asyncio.create_task(calculate_and_store_call_cost(CallSid, duration_seconds))
                        logger.info(f"[COST] Triggered cost calculation for call: {CallSid}")
                except Exception as cost_error:
                    logger.error(f"[COST] Failed to trigger cost calculation: {cost_error}")

        return {"message": "Status received"}

    except Exception as error:
        logger.error(f"Error in voice status callback: {str(error)}")
        import traceback
        logger.error(traceback.format_exc())
        return {"error": str(error)}


# ==================== SMS Webhook ====================

@router.api_route("/sms", methods=["GET", "POST"])
async def sms_webhook(
    request: Request,
    To: Optional[str] = Form(None),
    From: Optional[str] = Form(None),
    Body: Optional[str] = Form(None),
    MessageSid: Optional[str] = Form(None),
    AccountSid: Optional[str] = Form(None),
    NumMedia: Optional[str] = Form(None)
):
    """
    Dynamic SMS webhook router.

    This single endpoint handles ALL incoming SMS across all numbers.
    Routes messages based on the To number.

    Twilio sends these parameters:
    - To: Your Twilio number that received the SMS
    - From: Sender's phone number
    - Body: Message content
    - MessageSid: Unique message identifier
    - NumMedia: Number of media attachments (MMS)

    Args:
        request: FastAPI request
        To: Twilio number
        From: Sender number
        Body: Message text
        MessageSid: Message SID
        AccountSid: Account SID
        NumMedia: Number of media files

    Returns:
        HTMLResponse: TwiML response
    """
    try:
        logger.info(f"SMS webhook - To: {To}, From: {From}, Body: {Body[:50] if Body else 'None'}")

        if not To:
            # Fallback for GET
            To = request.query_params.get('To')
            From = request.query_params.get('From')
            Body = request.query_params.get('Body')
            MessageSid = request.query_params.get('MessageSid')

        if not To:
            logger.error("No 'To' parameter in SMS webhook")
            response = MessagingResponse()
            response.message("Error: Unable to process message.")
            return HTMLResponse(content=str(response), media_type="application/xml")

        db = Database.get_db()
        phone_numbers_collection = db['phone_numbers']
        assistants_collection = db['assistants']
        sms_logs_collection = db['sms_logs']

        # Look up the phone number
        phone_doc = phone_numbers_collection.find_one({"phone_number": To})

        if not phone_doc:
            logger.warning(f"SMS to unknown number: {To}")
            response = MessagingResponse()
            response.message("This number is not configured.")
            return HTMLResponse(content=str(response), media_type="application/xml")

        # Log the incoming SMS
        sms_log = {
            "message_sid": MessageSid,
            "to": To,
            "from": From,
            "body": Body,
            "num_media": int(NumMedia) if NumMedia else 0,
            "direction": "inbound",
            "phone_number_id": phone_doc["_id"],
            "created_at": datetime.utcnow()
        }

        # Check if assistant is assigned
        if phone_doc.get("assigned_assistant_id"):
            sms_log["assistant_id"] = phone_doc["assigned_assistant_id"]
            assistant_id = str(phone_doc["assigned_assistant_id"])

            # Fetch assistant
            assistant = assistants_collection.find_one({"_id": ObjectId(assistant_id)})
            if assistant:
                sms_log["assistant_name"] = assistant.get("name")

        sms_logs_collection.insert_one(sms_log)

        # For now, return a simple acknowledgment
        # TODO: In the future, you can integrate with OpenAI to generate intelligent responses
        response = MessagingResponse()

        if phone_doc.get("assigned_assistant_id"):
            response.message(f"Message received! This is handled by {assistant.get('name', 'our AI assistant')}. SMS responses coming soon!")
        else:
            response.message("Message received. This number is not yet configured with an AI assistant.")

        logger.info(f"SMS logged successfully - MessageSid: {MessageSid}")

        return HTMLResponse(content=str(response), media_type="application/xml")

    except Exception as error:
        import traceback
        logger.error(f"Error in SMS webhook: {str(error)}")
        logger.error(traceback.format_exc())

        response = MessagingResponse()
        response.message("Error processing your message. Please try again.")
        return HTMLResponse(content=str(response), media_type="application/xml")


@router.api_route("/sms-status", methods=["GET", "POST"])
async def sms_status_callback(
    request: Request,
    MessageSid: Optional[str] = Form(None),
    MessageStatus: Optional[str] = Form(None),
    To: Optional[str] = Form(None),
    From: Optional[str] = Form(None)
):
    """
    SMS status callback - receives SMS delivery status updates.

    Status values: queued, sending, sent, delivered, undelivered, failed

    Args:
        MessageSid: Message SID
        MessageStatus: Current status
        To: Recipient
        From: Sender

    Returns:
        dict: Success message
    """
    try:
        logger.info(f"SMS status - MessageSid: {MessageSid}, Status: {MessageStatus}")

        db = Database.get_db()
        sms_logs_collection = db['sms_logs']

        if MessageSid:
            sms_logs_collection.update_one(
                {"message_sid": MessageSid},
                {
                    "$set": {
                        "status": MessageStatus,
                        "updated_at": datetime.utcnow()
                    }
                }
            )

        return {"message": "Status received"}

    except Exception as error:
        logger.error(f"Error in SMS status callback: {str(error)}")
        import traceback
        logger.error(traceback.format_exc())
        return {"error": str(error)}


# Import datetime at the top
from datetime import datetime


# ==================== Campaign Webhooks ====================

@router.api_route("/outbound-call", methods=["GET", "POST"])
async def outbound_call_webhook(
    request: Request,
    leadId: Optional[str] = Form(None),
    campaignId: Optional[str] = Form(None),
    assistantId: Optional[str] = Form(None)
):
    """
    TwiML endpoint for outbound campaign calls.
    Connects the call to the assigned AI assistant.
    """
    try:
        # Try query params if form is empty
        if not leadId:
            leadId = request.query_params.get('leadId')
            campaignId = request.query_params.get('campaignId')
            assistantId = request.query_params.get('assistantId')

        logger.info(f"Outbound call - Lead: {leadId}, Campaign: {campaignId}, Assistant: {assistantId}")

        response = VoiceResponse()

        if not assistantId:
            response.say("Sorry, no assistant configured for this campaign.")
            return HTMLResponse(content=str(response), media_type="application/xml")

        # Connect to AI assistant via WebSocket
        # Use API_BASE_URL from settings for production, otherwise detect from request
        if settings.api_base_url:
            # Convert https:// to wss:// for WebSocket
            base_url = settings.api_base_url.replace('https://', '').replace('http://', '')
            stream_url = f'wss://{base_url}/api/outbound-calls/media-stream/{assistantId}'
        else:
            # Fallback to request hostname detection
            host = request.url.hostname
            if request.url.port and request.url.port not in [80, 443]:
                host = f"{host}:{request.url.port}"
            stream_url = f'wss://{host}/api/outbound-calls/media-stream/{assistantId}'

        logger.info(f"Connecting campaign call to assistant {assistantId} via {stream_url}")

        connect = Connect()
        query_params = []
        if campaignId:
            query_params.append(f"campaignId={campaignId}")
        if leadId:
            query_params.append(f"leadId={leadId}")
        if query_params:
            stream_url = f"{stream_url}?{'&'.join(query_params)}"
        connect.stream(url=stream_url)
        response.append(connect)

        return HTMLResponse(content=str(response), media_type="application/xml")

    except Exception as error:
        logger.error(f"Error in outbound call webhook: {error}")
        response = VoiceResponse()
        response.say("Sorry, an error occurred.")
        return HTMLResponse(content=str(response), media_type="application/xml")


@router.api_route("/call-status", methods=["GET", "POST"])
async def campaign_call_status(
    request: Request,
    CallSid: Optional[str] = Form(None),
    CallStatus: Optional[str] = Form(None),
    CallDuration: Optional[str] = Form(None),
    leadId: Optional[str] = Form(None),
    campaignId: Optional[str] = Form(None)
):
    """
    Campaign call status callback.
    Updates lead status and triggers next call on completion.
    """
    try:
        # Try query params
        if not CallSid:
            CallSid = request.query_params.get('CallSid')
            CallStatus = request.query_params.get('CallStatus')
            CallDuration = request.query_params.get('CallDuration')
            leadId = request.query_params.get('leadId')
            campaignId = request.query_params.get('campaignId')

        logger.info(f"[WEBHOOK] Campaign call status received - CallSid: {CallSid}, Status: {CallStatus}, Duration: {CallDuration}s, Lead: {leadId}, Campaign: {campaignId}")

        if not CallSid or not CallStatus:
            logger.error(f"[WEBHOOK] Missing required parameters - CallSid: {CallSid}, CallStatus: {CallStatus}")
            return {"error": "Missing required parameters"}

        # Process the status update (async for better performance)
        await process_call_status_async(CallSid, CallStatus, CallDuration, leadId, campaignId)
        logger.info(f"[WEBHOOK] Successfully processed call status for CallSid: {CallSid}")

        # Calculate cost for completed calls
        if CallStatus == "completed" and CallDuration:
            try:
                from app.services.cost_calculator import calculate_and_store_call_cost
                import asyncio
                duration_seconds = int(CallDuration)
                if duration_seconds > 0:
                    asyncio.create_task(calculate_and_store_call_cost(CallSid, duration_seconds))
                    logger.info(f"[COST] Triggered cost calculation for campaign call: {CallSid}")
            except Exception as cost_error:
                logger.error(f"[COST] Failed to trigger cost calculation: {cost_error}")

        return {"message": "Status updated"}

    except Exception as error:
        logger.error(f"[WEBHOOK] Error in campaign call status: {error}")
        import traceback
        logger.error(traceback.format_exc())
        return {"error": str(error)}


@router.api_route("/recording", methods=["GET", "POST"])
async def campaign_recording_callback(
    request: Request,
    RecordingSid: Optional[str] = Form(None),
    RecordingUrl: Optional[str] = Form(None),
    CallSid: Optional[str] = Form(None),
    RecordingStatus: Optional[str] = Form(None),
    RecordingDuration: Optional[str] = Form(None),
    leadId: Optional[str] = Form(None),
    campaignId: Optional[str] = Form(None)
):
    """
    Campaign recording callback.
    Stores recording URL and triggers post-call AI processing.
    """
    try:
        # Try query params
        if not RecordingSid:
            RecordingSid = request.query_params.get('RecordingSid')
            RecordingUrl = request.query_params.get('RecordingUrl')
            CallSid = request.query_params.get('CallSid')
            RecordingStatus = request.query_params.get('RecordingStatus')
            RecordingDuration = request.query_params.get('RecordingDuration')
            leadId = request.query_params.get('leadId')
            campaignId = request.query_params.get('campaignId')

        logger.info(f"Recording callback - RecordingSid: {RecordingSid}, CallSid: {CallSid}, Status: {RecordingStatus}")

        if not RecordingSid or not CallSid:
            return {"error": "Missing required parameters"}

        # Add .mp3 extension to recording URL for direct download
        recording_mp3_url = f"{RecordingUrl}.mp3" if RecordingUrl else None

        db = Database.get_db()
        call_attempts_collection = db["call_attempts"]
        call_logs_collection = db["call_logs"]

        # Update call attempt with recording info
        update_result = call_attempts_collection.update_one(
            {"call_sid": CallSid},
            {
                "$set": {
                    "recording_url": recording_mp3_url,
                    "recording_sid": RecordingSid,
                    "recording_status": RecordingStatus,
                    "recording_duration": int(RecordingDuration) if RecordingDuration else None,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        # Also update call_logs with recording URL.
        call_logs_result = call_logs_collection.update_one(
            {"call_sid": CallSid},
            {
                "$set": {
                    "recording_url": recording_mp3_url,
                    "recording_sid": RecordingSid,
                    "recording_status": RecordingStatus,
                    "recording_duration": int(RecordingDuration) if RecordingDuration else None,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        # Per-collection persistence logs — earlier code gated this log on the
        # call_attempts match alone, so dashboard / inbound calls (which only
        # have a call_logs row) silently looked like "no URL saved" even when
        # the URL was saved correctly. Log each collection independently.
        if update_result.matched_count > 0:
            logger.info(f"[RECORDING] saved on call_attempts for CallSid={CallSid}")
        if call_logs_result.matched_count > 0:
            logger.info(f"[RECORDING] saved on call_logs for CallSid={CallSid}")
        elif update_result.matched_count == 0:
            # Neither collection matched — this is a real anomaly. Either the
            # call_sid was never logged (race / API outage during dial) or
            # Twilio is firing a recording callback for a call we never made.
            logger.warning(
                f"[RECORDING] orphan recording callback for CallSid={CallSid} — "
                f"no row in call_logs or call_attempts"
            )

        # Trigger post-call processing for completed recordings (async optimized)
        if RecordingStatus == "completed" and recording_mp3_url:
            try:
                from app.services.async_post_call_processor import AsyncPostCallProcessor
                processor = AsyncPostCallProcessor()
                import asyncio

                # Always trigger transcription for all calls (both realtime and custom provider modes)
                # Twilio native transcription doesn't work with <Stream> verb used by WebSocket calls
                #
                # Chained: transcription THEN summary extraction. The summary
                # step depends on `call_logs.transcription` being populated,
                # which the transcriber writes inside `transcribe_and_update_call`.
                # Without chaining, the prior fire-and-forget transcription
                # would race the summary-trigger and the summary would see an
                # empty transcript. (QA found this — `_trigger_transcription_after_delay`
                # was hooked but the live path bypassed it entirely.)
                logger.info(f"Triggering automatic transcription for call: {CallSid}")
                asyncio.create_task(_transcribe_and_summarize(processor, CallSid, recording_mp3_url))

                # If this is a campaign call, also trigger post-call AI processing (sentiment/summary)
                if leadId and campaignId:
                    logger.info(f"Triggering post-call processing for campaign call: {CallSid}")
                    asyncio.create_task(processor.process_call(CallSid, leadId, campaignId))

            except Exception as e:
                logger.error(f"Error triggering post-call processing: {e}")

        return {"message": "Recording saved"}

    except Exception as error:
        logger.error(f"Error in recording callback: {error}")
        return {"error": str(error)}
