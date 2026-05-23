"""Unified Twilio voice-routing for all phone numbers under management.

The platform has ONE inbound webhook URL — `/api/twilio-webhooks/voice` — that
dynamically routes every incoming call by looking up the assigned assistant
for the dialed `To` number in Mongo. This module is the single point of
truth that ALL number-management code paths call when a number enters our
control (purchase / connect-provider import / refresh / assignment), so we
never end up with numbers configured against the wrong webhook.

Background
----------
Twilio numbers can be routed three ways:
  1. `voice_url` — the URL Twilio POSTs to when the number receives a call
  2. `voice_application_sid` — a TwiML Application SID; if set, Twilio uses
     the App's URL and IGNORES `voice_url`
  3. Neither — Twilio plays a default error and hangs up

Convis's design is **(1) only** — a single platform-wide webhook on every
number's `voice_url`. We explicitly clear `voice_application_sid` because
TwiML Apps would override our voice_url and silently break "Change AI"
(an actual production bug — numbers purchased through the dashboard were
created with TwiML Apps that pointed at a deprecated WebSocket pipeline,
and "Change AI" updates to voice_url were silently no-op).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config.settings import settings

logger = logging.getLogger(__name__)


# Path of the unified inbound voice webhook. Anything else on a Convis-managed
# Twilio number is misconfigured — that's literally the contract between
# Convis and Twilio for every inbound call.
UNIFIED_VOICE_PATH = "/api/twilio-webhooks/voice"
UNIFIED_VOICE_STATUS_PATH = "/api/twilio-webhooks/voice-status"


def unified_voice_url() -> Optional[str]:
    """Compute the public URL Twilio should POST to when an inbound call
    arrives. Returns None if api_base_url isn't configured (caller should
    log loud and skip the Twilio update — better than writing junk URLs)."""
    base = settings.api_base_url or settings.base_url
    if not base:
        return None
    return f"{base.rstrip('/')}{UNIFIED_VOICE_PATH}"


def unified_voice_status_url() -> Optional[str]:
    base = settings.api_base_url or settings.base_url
    if not base:
        return None
    return f"{base.rstrip('/')}{UNIFIED_VOICE_STATUS_PATH}"


def ensure_unified_voice_routing(twilio_client, provider_sid: str, *, label: str = "") -> tuple[bool, str]:
    """Idempotently set a Twilio number's voice routing to the platform's
    unified webhook.

    Args:
        twilio_client: An authenticated `twilio.rest.Client` for the account
            that owns this number. Caller is responsible for using the right
            account's credentials (numbers can only be re-routed by their
            owning Twilio account).
        provider_sid: The Twilio `PNxxxxxxxx` SID of the phone number.
        label: Human-readable context for logs (typically the E.164 number).

    Returns:
        (ok: bool, message: str) — `ok=False` is non-fatal (we don't want
        a Twilio API blip to fail an import of 50 numbers); caller logs +
        continues with the next number.
    """
    voice_url = unified_voice_url()
    status_cb = unified_voice_status_url()

    if not voice_url:
        msg = (
            f"[VOICE_ROUTING] api_base_url unset; refusing to write voice_url for "
            f"{label or provider_sid}. Set API_BASE_URL on the API and retry."
        )
        logger.error(msg)
        return False, msg

    try:
        # Set voice_url + clear voice_application_sid in one round-trip.
        # Without the App-SID clear, "voice_url" would silently be ignored
        # on numbers Twilio has previously associated with a TwiML App.
        twilio_client.incoming_phone_numbers(provider_sid).update(
            voice_url=voice_url,
            voice_method="POST",
            voice_application_sid="",          # detach any TwiML App
            status_callback=status_cb,
            status_callback_method="POST",
        )
        logger.info(
            "[VOICE_ROUTING] %s (sid=%s) → %s",
            label or provider_sid, provider_sid, voice_url,
        )
        return True, voice_url
    except Exception as exc:
        msg = (
            f"[VOICE_ROUTING] Failed to update {label or provider_sid} (sid={provider_sid}): {exc}"
        )
        logger.warning(msg)
        return False, str(exc)
