"""Public demo-call endpoint — no auth, heavy abuse-protection.

Called from the marketing site (convis.ai) when a visitor enters their phone
number in the "Talk to our AI — right now" widget. Triggers an outbound call
from a dedicated demo number (admin's CONVISLABS assistant) to the visitor.

No JWT required, but the route MUST defend itself against:
  * cost abuse (each call costs us money) — global cap + per-IP cap
  * harassment (someone enters a victim's number) — per-target-number cap +
    explicit consent text on the form
  * toll-fraud (premium-rate / international scam ranges) — prefix denylist

Defaults below are conservative; tune via env vars if real demand outgrows
them. Rate-limit state lives in Mongo so it survives restarts and is shared
across multiple API replicas.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config.database import Database
from app.config.settings import settings
from app.middleware.rate_limiter import limiter
from app.services.livekit.assistant_config import load_assistant_config
from app.services.livekit.sip_service import (
    create_room_with_agent,
    generate_room_name,
    hangup_room,
)
from app.services.livekit.tokens import LiveKitNotConfigured
from app.services.twilio_outbound import TwilioNotConfigured, dial_outbound_via_twilio

logger = logging.getLogger(__name__)
router = APIRouter()

# Min 7 digits, max 15 — stricter than full E.164 to avoid super-short test
# numbers and keep prefix-based denylist meaningful.
E164 = re.compile(r"^\+[1-9]\d{6,14}$")

# Demo call uses this number (admin's CONVISLABS assistant). Override via env
# for environments where you want a different demo persona.
DEMO_FROM_NUMBER = os.getenv("DEMO_FROM_NUMBER", "+16592655550")

# Country-code ALLOWLIST. A public, cost-bearing, unauthenticated endpoint
# requires allowlist (not denylist) — any prefix we forget to deny is a free
# toll-fraud / sanctions-violation vector.
#
# Each entry is a country dial-code prefix WITHOUT the leading +. Override
# via DEMO_ALLOWED_PREFIXES env var (comma-separated).
_DEFAULT_ALLOWED = (
    "1",     # US, Canada (NANP — Caribbean premium ranges blocked separately below)
    "44",    # United Kingdom
    "91",    # India
    "971",   # United Arab Emirates
    "65",    # Singapore
    "61",    # Australia
    "49",    # Germany
    "33",    # France
    "34",    # Spain
    "39",    # Italy (Italy 199 premium blocked separately below)
    "31",    # Netherlands
    "46",    # Sweden
    "47",    # Norway
    "353",   # Ireland
    "351",   # Portugal
    "64",    # New Zealand
    "966",   # Saudi Arabia
    "974",   # Qatar
    "60",    # Malaysia
    "63",    # Philippines
    "62",    # Indonesia
    "66",    # Thailand
    "84",    # Vietnam
    "81",    # Japan
    "82",    # South Korea
    "852",   # Hong Kong
    "886",   # Taiwan
    "27",    # South Africa
    "55",    # Brazil
    "52",    # Mexico
    "56",    # Chile
    "57",    # Colombia
    "54",    # Argentina
)
ALLOWED_PREFIXES = tuple(
    p.strip().lstrip("+")
    for p in os.getenv("DEMO_ALLOWED_PREFIXES", ",".join(_DEFAULT_ALLOWED)).split(",")
    if p.strip()
)

# Even within allowed countries, certain ranges are premium-rate or sanctioned
# — block specific prefixes regardless of allowlist match.
_BLOCKED_SPECIFIC = (
    # NANP (+1) Caribbean / 900-class ranges that bill like premium
    "+1900", "+1976", "+1809", "+1268", "+1284", "+1473", "+1649", "+1664",
    "+1670", "+1758", "+1767", "+1784", "+1829", "+1849", "+1868", "+1869", "+1876",
    # UK premium-rate (£1-£3.60/min)
    "+4490", "+4491",
    # Italy premium-rate
    "+39199", "+39144", "+39166", "+39709",
    # Germany premium 0900
    "+49900",
    # Australia premium 19xx
    "+6119",
    # France premium 0890-0899
    "+33890", "+33891", "+33892", "+33893", "+33894", "+33895", "+33896",
    "+33897", "+33898", "+33899",
)


def _country_prefix_matches(number: str) -> bool:
    """Return True iff `number`'s country code is in the allowlist."""
    if not number.startswith("+"):
        return False
    digits = number[1:]
    return any(digits.startswith(p) for p in ALLOWED_PREFIXES)

# Per-IP and per-target-number windows (24h)
PER_IP_LIMIT = int(os.getenv("DEMO_PER_IP_LIMIT", "3"))
PER_NUMBER_LIMIT = int(os.getenv("DEMO_PER_NUMBER_LIMIT", "3"))
GLOBAL_DAILY_LIMIT = int(os.getenv("DEMO_GLOBAL_DAILY_LIMIT", "100"))

# OTP-request rate limits — separate from call rate limits, to prevent SMS spam.
OTP_PER_NUMBER_PER_HOUR = int(os.getenv("DEMO_OTP_PER_NUMBER_PER_HOUR", "5"))
OTP_PER_IP_PER_HOUR = int(os.getenv("DEMO_OTP_PER_IP_PER_HOUR", "10"))


class OtpRequest(BaseModel):
    to_number: str = Field(..., description="Phone number to send the OTP to — E.164 preferred")


class DemoCallRequest(BaseModel):
    to_number: str = Field(..., description="Phone number to dial — E.164 preferred (e.g. +14155551234)")
    otp: str = Field(..., min_length=4, max_length=10, description="6-digit code from the SMS")


class DemoCallResponse(BaseModel):
    status: str
    message: str
    call_sid: Optional[str] = None


def _normalize_phone(raw: str) -> str:
    """Strip whitespace/punctuation. Add +1 country code if user typed bare US digits."""
    s = (raw or "").strip()
    s = re.sub(r"[\s\-\(\)\.]", "", s)
    if not s:
        return s
    if not s.startswith("+"):
        # Heuristic: 10 digits → US, else require explicit country code
        digits = re.sub(r"\D", "", s)
        if len(digits) == 10:
            s = "+1" + digits
        elif len(digits) == 11 and digits.startswith("1"):
            s = "+" + digits
        else:
            # Don't guess — let validation reject so the user is forced to be explicit.
            s = "+" + digits
    return s


def _client_ip(request: Request) -> str:
    """Anti-spoofing client IP — see app/middleware/rate_limiter.py for the
    full rationale. Take LAST entry of X-Forwarded-For (App Runner appends
    the real TCP source, earlier entries are attacker-controlled)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        ips = [ip.strip() for ip in fwd.split(",") if ip.strip()]
        if ips:
            return ips[-1]
    return request.client.host if request.client else "unknown"


def _get_demo_twilio_creds():
    """Resolve the Twilio account that owns the demo number. We use the same
    credentials Twilio Verify will SMS from — the demo number's owning user.

    Returns (account_sid, auth_token) or (None, None) if unavailable.
    """
    from app.utils.twilio_helpers import decrypt_twilio_credentials, CredentialDecryptionError
    db = Database.get_db()
    phone_doc = db["phone_numbers"].find_one({"phone_number": DEMO_FROM_NUMBER})
    if not phone_doc:
        return None, None
    conn = db["provider_connections"].find_one({"user_id": phone_doc["user_id"], "provider": "twilio"})
    if not conn:
        return None, None
    try:
        return decrypt_twilio_credentials(conn)
    except CredentialDecryptionError:
        logger.error("[DEMO] Cannot decrypt Twilio creds for demo number owner")
        return None, None


def _get_or_create_verify_service(twilio_client) -> Optional[str]:
    """Lazy-bootstrap a Twilio Verify Service for OTP delivery.

    Service SID is cached in Mongo (`settings/demo_verify`) so subsequent
    requests reuse the same service. If the cached SID is stale (manually
    deleted in Twilio console) the cache is refreshed transparently.
    """
    db = Database.get_db()
    cfg = db["settings"].find_one({"_id": "demo_verify"})
    sid = cfg.get("verify_service_sid") if cfg else None

    if sid:
        # Verify the cached service still exists. Twilio raises if not.
        try:
            twilio_client.verify.v2.services(sid).fetch()
            return sid
        except Exception:
            logger.warning("[DEMO] Cached Verify Service %s no longer exists; recreating", sid)

    try:
        service = twilio_client.verify.v2.services.create(
            friendly_name="Convis Demo Verification",
            code_length=6,
        )
    except Exception as exc:
        logger.error("[DEMO] Failed to create Verify Service: %s", exc)
        return None

    db["settings"].update_one(
        {"_id": "demo_verify"},
        {"$set": {
            "verify_service_sid": service.sid,
            "created_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )
    logger.info("[DEMO] Created Verify Service %s", service.sid)
    return service.sid


@router.post("/demo-call/request-otp", status_code=status.HTTP_200_OK)
@limiter.limit("20/hour")  # IP burst guard on top of per-IP-hour cap
async def request_otp(request: Request, body: OtpRequest):
    """Send a 6-digit OTP via SMS to the visitor's phone number.

    Rate-limited per-number AND per-IP to block SMS-spam abuse — Twilio Verify
    is ~5¢ per send, so an unbounded endpoint is a classic toll-fraud vector.
    """
    to_number = _normalize_phone(body.to_number)
    if not E164.match(to_number):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please enter a valid phone number with country code (e.g. +14155551234).",
        )
    # Allowlist: country must be in our supported list.
    if not _country_prefix_matches(to_number):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sorry, that country isn't supported for demo calls yet. "
                   "Email sales@convis.ai if you'd like us to add it.",
        )
    # Specific premium-rate / pay-per-call sub-ranges (blocked even within
    # allowed countries — e.g. UK 09xx, Italy 199, US 900).
    if any(to_number.startswith(p) for p in _BLOCKED_SPECIFIC):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sorry, that number range isn't supported for demo calls.",
        )

    ip = _client_ip(request)
    now = datetime.now(timezone.utc)
    cutoff_hour = now - timedelta(hours=1)

    db = Database.get_db()
    otp_log = db["demo_otp_requests"]

    if otp_log.count_documents({"to_number": to_number, "created_at": {"$gte": cutoff_hour}}) >= OTP_PER_NUMBER_PER_HOUR:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many OTP requests for this number. Please wait an hour and try again.",
        )
    if otp_log.count_documents({"ip": ip, "created_at": {"$gte": cutoff_hour}}) >= OTP_PER_IP_PER_HOUR:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many OTP requests from this connection. Please wait an hour.",
        )

    # Same-day demo-call quota check — fail fast before sending SMS, otherwise
    # users waste an SMS on a phone that already hit the 24h call limit.
    cutoff_24h = now - timedelta(hours=24)
    if db["demo_call_attempts"].count_documents({"to_number": to_number, "created_at": {"$gte": cutoff_24h}}) >= PER_NUMBER_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="This number already received the maximum demo calls for today. Try again in 24 hours.",
        )

    sid, token = _get_demo_twilio_creds()
    if not sid or not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo verification is temporarily unavailable.",
        )
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioRestException
    client = Client(sid, token)
    service_sid = _get_or_create_verify_service(client)
    if not service_sid:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo verification service unavailable.",
        )

    try:
        client.verify.v2.services(service_sid).verifications.create(
            to=to_number, channel="sms",
        )
    except TwilioRestException as exc:
        logger.warning("[DEMO] Twilio Verify failed for %s: %s", to_number, exc)
        # Twilio rejects unreachable / invalid numbers with code 60200 / 60205 etc.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Couldn't send a code to that number. Please double-check it.",
        )
    except Exception as exc:
        logger.error("[DEMO] OTP send failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Couldn't send the verification code. Please try again.",
        )

    otp_log.insert_one({
        "to_number": to_number,
        "ip": ip,
        "created_at": now,
    })
    logger.info("[DEMO] OTP sent to %s (ip=%s)", to_number, ip)

    return {"status": "otp_sent", "message": "We sent a 6-digit code to your phone."}


@router.post("/demo-call", response_model=DemoCallResponse, status_code=status.HTTP_200_OK)
@limiter.limit("10/hour")  # IP burst limit on top of the per-IP-24h cap below
async def demo_call(request: Request, body: DemoCallRequest):
    """Place a one-shot demo call to the visitor.

    Requires a valid OTP previously sent via /demo-call/request-otp. Without
    OTP verification, attackers could enter a victim's number and the bot
    would call them — TCPA risk. Twilio Verify proves the visitor actually
    has access to the destination phone before we dial.
    """
    to_number = _normalize_phone(body.to_number)
    otp = (body.otp or "").strip()

    # --- Validation ---
    if not E164.match(to_number):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please enter a valid phone number with country code (e.g. +14155551234).",
        )
    # Allowlist: country must be in our supported list.
    if not _country_prefix_matches(to_number):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sorry, that country isn't supported for demo calls yet. "
                   "Email sales@convis.ai if you'd like us to add it.",
        )
    # Specific premium-rate / pay-per-call sub-ranges (blocked even within
    # allowed countries — e.g. UK 09xx, Italy 199, US 900).
    if any(to_number.startswith(p) for p in _BLOCKED_SPECIFIC):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sorry, that number range isn't supported for demo calls.",
        )
    # Strict ASCII-digit check. Python's str.isdigit() returns True for
    # Devanagari (१२३४), Arabic-Indic (٠١٢٣), Bengali, etc. — those don't
    # match what Twilio sent, and let bad payloads reach the verify step
    # where they crash at the API boundary (HTTP 500 instead of 400).
    if not re.fullmatch(r"[0-9]{4,10}", otp):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please enter the 6-digit code from the SMS.",
        )

    # --- OTP verification (Twilio Verify) ---
    sid, token = _get_demo_twilio_creds()
    if not sid or not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo verification is temporarily unavailable.",
        )
    from twilio.rest import Client
    from twilio.base.exceptions import TwilioRestException
    twilio_client = Client(sid, token)
    service_sid = _get_or_create_verify_service(twilio_client)
    if not service_sid:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo verification service unavailable.",
        )
    try:
        check = twilio_client.verify.v2.services(service_sid).verification_checks.create(
            to=to_number, code=otp,
        )
    except TwilioRestException as exc:
        # Twilio 20404 covers several user-error scenarios. We can't always
        # tell them apart from the error body, so the message has to be
        # generic enough to cover all of: expired (10 min) / already approved
        # (consumed) / max-attempts-reached (5 wrong codes auto-cancels).
        logger.warning("[DEMO] OTP check error for %s: %s", to_number, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That code didn't work — it may be wrong, expired, or already used. Request a fresh code and try again.",
        )
    if check.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That code didn't match. Double-check the SMS and try again.",
        )

    ip = _client_ip(request)
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    db = Database.get_db()
    attempts = db["demo_call_attempts"]

    # --- Abuse limits (Mongo-backed so they hold across replicas / restarts) ---
    if attempts.count_documents({"to_number": to_number, "created_at": {"$gte": cutoff_24h}}) >= PER_NUMBER_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="This number already received a demo call recently. Try again in 24 hours.",
        )
    if attempts.count_documents({"ip": ip, "created_at": {"$gte": cutoff_24h}}) >= PER_IP_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="You've already used your daily demo calls. Please try again in 24 hours.",
        )
    if attempts.count_documents({"created_at": {"$gte": cutoff_24h}}) >= GLOBAL_DAILY_LIMIT:
        logger.warning("[DEMO] Global daily cap hit (%s); refusing further calls", GLOBAL_DAILY_LIMIT)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo line is at capacity right now. Please try again in a few hours.",
        )

    # --- Resolve demo number → owning assistant ---
    phone_doc = db["phone_numbers"].find_one({"phone_number": DEMO_FROM_NUMBER})
    if not phone_doc or not phone_doc.get("assigned_assistant_id"):
        logger.error("[DEMO] %s missing or has no assigned_assistant_id", DEMO_FROM_NUMBER)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo service is temporarily unavailable. Please try again later.",
        )

    owner_user_id = phone_doc["user_id"]
    assistant_obj_id = phone_doc["assigned_assistant_id"]
    assistant = db["assistants"].find_one({"_id": assistant_obj_id})
    if not assistant:
        logger.error("[DEMO] Assistant %s not found", assistant_obj_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo assistant unavailable.",
        )

    try:
        config = load_assistant_config(str(assistant_obj_id))
    except ValueError as exc:
        logger.error("[DEMO] Assistant config load failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo assistant config invalid.",
        )

    # --- Provisional call_log row (insert before dial so partial failures still leave a trace) ---
    room_name = generate_room_name(prefix="pstn-out")
    initial_log = {
        "user_id": owner_user_id,
        "assistant_id": assistant_obj_id,
        "assistant_name": assistant.get("name"),
        "phone_number": phone_doc["_id"],
        "phone_number_value": DEMO_FROM_NUMBER,
        "call_sid": room_name,  # provisional; replaced with twilio_call_sid post-dial
        "twilio_call_sid": None,
        "livekit_room": room_name,
        "direction": "outbound",
        "from_number": DEMO_FROM_NUMBER,
        "to_number": to_number,
        "status": "initiating",
        # Demo-specific tracing for abuse forensics + analytics
        "source": "demo",
        "demo_caller_ip": ip,
        "voice_config": {
            "transport": "twilio-twiml",
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
    log_id = db["call_logs"].insert_one(initial_log).inserted_id

    # --- Provision LiveKit room with agent + place outbound dial ---
    twilio_call_sid: Optional[str] = None
    try:
        await create_room_with_agent(
            room_name=room_name,
            assistant_config=config,
            metadata_extra={
                "source": "pstn",
                "direction": "outbound",
                "to_number": to_number,
                "from_number": DEMO_FROM_NUMBER,
                "demo": True,
            },
        )

        base = settings.api_base_url or settings.base_url
        status_cb = recording_cb = None
        if base:
            base = base.rstrip("/")
            status_cb = f"{base}/webhooks/twilio/calls"
            recording_cb = f"{base}/api/twilio-webhooks/recording"

        twilio_call_sid = await dial_outbound_via_twilio(
            room_name=room_name,
            phone_number=to_number,
            caller_id=DEMO_FROM_NUMBER,
            status_callback_url=status_cb,
            recording_callback_url=recording_cb,
        )
    except (LiveKitNotConfigured, TwilioNotConfigured) as exc:
        db["call_logs"].update_one(
            {"_id": log_id},
            {"$set": {"status": "failed", "failure_reason": str(exc), "updated_at": datetime.now(timezone.utc)}},
        )
        try:
            await hangup_room(room_name)
        except Exception:
            pass
        logger.error("[DEMO] Provider not configured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo service is temporarily unavailable.",
        )
    except Exception as exc:
        logger.error("[DEMO] Outbound dial failed", exc_info=True)
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
            detail="Couldn't place the demo call right now. Please try again.",
        )

    # --- Success ---
    db["call_logs"].update_one(
        {"_id": log_id},
        {"$set": {
            "call_sid": twilio_call_sid or room_name,
            "twilio_call_sid": twilio_call_sid,
            "status": "initiated",
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    # Record the rate-limit attempt (only AFTER successful dial — failed dials
    # don't count against the visitor's daily quota, which is the friendlier UX).
    attempts.insert_one({
        "ip": ip,
        "to_number": to_number,
        "created_at": now,
        "call_sid": twilio_call_sid or room_name,
    })

    logger.info(
        "[DEMO] Placed demo call to %s from IP %s (call_sid=%s)",
        to_number, ip, twilio_call_sid or room_name,
    )

    return DemoCallResponse(
        status="initiated",
        message="Calling you now! The demo agent will dial in a few seconds.",
        call_sid=twilio_call_sid or room_name,
    )
