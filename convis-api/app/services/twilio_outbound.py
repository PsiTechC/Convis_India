"""Outbound PSTN dial via Twilio Programmable Voice (no Elastic SIP Trunk).

Flow:
    1. Backend creates a LiveKit room and dispatches the agent (handled in
       sip_service.create_room_with_agent — same as the SIP-trunk path).
    2. Backend asks Twilio to place a PSTN call to the destination number.
       The TwiML returned to Twilio bridges the answered leg to LiveKit's SIP
       ingress via <Dial><Sip>sip:room@livekit-host</Sip></Dial>.
    3. When the callee picks up, Twilio dials LiveKit SIP, which lands the
       caller in the same room as the agent.

This path requires only TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN — NO Elastic
SIP Trunk and NO LiveKit Outbound Trunk. It costs slightly more per minute
(Programmable Voice ~$0.014/min vs Elastic SIP ~$0.0085/min in US) and adds
a TwiML interpretation hop, but works out-of-the-box.

Use sip_service.dial_outbound_sip when LIVEKIT_SIP_OUTBOUND_TRUNK_ID is set
for the cheaper, lower-latency direct-SIP path.

ASYNC SAFETY: the official Twilio SDK is sync. We run all HTTP calls inside
asyncio.to_thread so they don't block the FastAPI event loop. Every call
also enforces a hard timeout via the SDK's HTTP client config.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client
from twilio.twiml.voice_response import Dial, VoiceResponse

from app.config.settings import settings
from app.services.livekit.tokens import LiveKitNotConfigured

logger = logging.getLogger(__name__)

# Hard timeout on every Twilio HTTP request. Long enough for slow PSTN
# carrier dial-up (~6 s typical), short enough to fail fast on outage.
_TWILIO_HTTP_TIMEOUT_S = 10


class TwilioNotConfigured(RuntimeError):
    pass


def _require_twilio_creds() -> tuple[str, str]:
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        raise TwilioNotConfigured(
            "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are required for "
            "Twilio Programmable Voice outbound dialing"
        )
    return settings.twilio_account_sid, settings.twilio_auth_token


def _require_livekit_sip_host() -> str:
    if not settings.livekit_sip_inbound_host:
        raise LiveKitNotConfigured(
            "LIVEKIT_SIP_INBOUND_HOST must be set so Twilio knows where to "
            "bridge the answered call leg"
        )
    return settings.livekit_sip_inbound_host


def _twilio_client() -> Client:
    """Build a Twilio Client with an explicit HTTP timeout. Default SDK has
    no timeout — a slow Twilio API would block forever."""
    sid, token = _require_twilio_creds()
    http = TwilioHttpClient(timeout=_TWILIO_HTTP_TIMEOUT_S)
    return Client(sid, token, http_client=http)


def build_bridge_twiml(room_name: str, recording_callback_url: Optional[str] = None) -> str:
    """TwiML that bridges Twilio's answered leg to a LiveKit room via SIP.

    answer_on_bridge=True: caller hears nothing until LiveKit answers, which
    avoids the brief Twilio "click" gap and matches LiveKit room latency.

    record="record-from-answer-dual": records both legs (caller + agent) from
    the moment the call is answered, in dual-channel format. Recording is
    delivered asynchronously to recording_callback_url when finalized
    (typically within a few seconds of call end). Without this, call_logs
    would never get a recording_url because Twilio doesn't auto-record bridges.
    """
    sip_uri = f"sip:{room_name}@{_require_livekit_sip_host()}"
    response = VoiceResponse()
    dial_kwargs = {"answer_on_bridge": True}
    if recording_callback_url:
        dial_kwargs["record"] = "record-from-answer-dual"
        dial_kwargs["recording_status_callback"] = recording_callback_url
        dial_kwargs["recording_status_callback_event"] = "completed"
        dial_kwargs["recording_status_callback_method"] = "POST"
    dial = Dial(**dial_kwargs)
    dial.sip(sip_uri)
    response.append(dial)
    return str(response)


async def verify_caller_id_owned(caller_id: str) -> bool:
    """Return True if `caller_id` is a phone number on this Twilio account.

    Prevents caller-id spoofing — without this, a user with a tampered
    phone_numbers DB row could dial out using a number they don't actually
    own on Twilio (which Twilio would reject anyway, but with a generic
    error that masks the mis-config).
    """
    client = _twilio_client()

    def _check():
        nums = client.incoming_phone_numbers.list(phone_number=caller_id, limit=1)
        return len(nums) > 0

    try:
        return await asyncio.to_thread(_check)
    except Exception as exc:
        logger.warning("[TWILIO] caller_id verification failed for %s: %s", caller_id, exc)
        # If Twilio is unreachable, don't block the call — log and proceed.
        # Twilio will reject the dial itself if the number really isn't ours.
        return True


async def dial_outbound_via_twilio(
    *,
    room_name: str,
    phone_number: str,
    caller_id: str,
    status_callback_url: Optional[str] = None,
    recording_callback_url: Optional[str] = None,
) -> str:
    """Place a PSTN call via Twilio that bridges to LiveKit room when answered.

    Returns the Twilio Call SID.

    If recording_callback_url is provided, the bridge is recorded (both legs)
    and Twilio POSTs the recording metadata to that URL when ready. Our
    recording webhook (twilio_webhooks/webhooks.py) updates both call_attempts
    and call_logs with recording_url + recording_sid + duration, then triggers
    automatic transcription via AsyncPostCallProcessor.
    """
    twiml = build_bridge_twiml(room_name, recording_callback_url=recording_callback_url)
    client = _twilio_client()

    create_kwargs = {
        "to": phone_number,
        "from_": caller_id,
        "twiml": twiml,
    }
    if status_callback_url:
        create_kwargs["status_callback"] = status_callback_url
        create_kwargs["status_callback_event"] = ["initiated", "ringing", "answered", "completed"]
        create_kwargs["status_callback_method"] = "POST"

    def _create():
        return client.calls.create(**create_kwargs)

    call = await asyncio.to_thread(_create)
    logger.info(
        "[TWILIO] Outbound call %s placed to %s (from %s) bridging to room %s",
        call.sid, phone_number, caller_id, room_name,
    )
    return call.sid


async def hangup_twilio_call(call_sid: str) -> None:
    """End an in-progress Twilio call by updating it to status='completed'."""
    client = _twilio_client()

    def _hangup():
        return client.calls(call_sid).update(status="completed")

    try:
        await asyncio.to_thread(_hangup)
        logger.info("[TWILIO] Call %s hung up", call_sid)
    except Exception as exc:
        # Twilio raises if the call is already completed — non-fatal.
        logger.warning("[TWILIO] Could not hang up call %s: %s", call_sid, exc)
