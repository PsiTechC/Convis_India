"""Outbound follow-up workflow service.

Backend for the "tax-attorney style" outbound call flow:

  bot dials client →
    asks: "did you file your taxes?" →
      if NO  → offers to book an appointment with the CA →
              on confirm: writes a real calendar event +
              fires two WhatsApp template messages (one to the client, one
              to the CA) →
      if YES → records the outcome, wraps up.

The `agent_worker` exposes two @function_tool methods (`record_filing_status`
and `book_followup_appointment`) that are thin wrappers around the async
functions in this module. Keeping the heavy lifting here:
  • lets the agent worker stay focused on the audio pipeline,
  • makes the workflow testable without spinning up LiveKit,
  • is the natural home for any future "outbound follow-up" variants
    (renewal reminders, payment confirmations, survey calls, etc.).

Opt-in per assistant. The wrapper tools no-op when
`outbound_followup_enabled` is False on the assistant config, so other
assistants are unaffected by this module being imported.

------------------------------------------------------------------
Assistant config fields consumed (all live on the assistant doc in Mongo —
the manager fills these in when wiring up the actual bot; this module just
reads them):

  outbound_followup_enabled       : bool       — master switch
  outbound_followup_topic         : str        — short label for prompts/audit
                                                 (e.g. "tax filing", "renewal")
  ca_name                         : str        — the professional being booked
                                                 (e.g. "Ms. Iyer (CA)")
  ca_phone                        : str (E.164)— for the CA-side WhatsApp ping
  ca_calendar_account_id          : str        — the CA's connected Google
                                                 Calendar account in
                                                 `calendar_accounts`
  firm_name                       : str        — for the WhatsApp template body
  wa_template_client              : str        — APPROVED WhatsApp template
                                                 name for the client confirm
  wa_template_ca                  : str        — APPROVED WhatsApp template
                                                 name for the CA notify
  appointment_duration_minutes    : int        — default 30
  appointment_timezone            : str        — IANA tz (e.g. "Asia/Kolkata")

------------------------------------------------------------------
Recommended WhatsApp template SHAPE (submit to Meta):

  template name: <wa_template_client>
    Body: "Hi {{1}}, this is {{2}}. Your tax consultation with {{3}} is
           confirmed for {{4}} at {{5}}. Reply CANCEL to release the slot."
    Params (in order): client_name, firm_name, ca_name, date, time

  template name: <wa_template_ca>
    Body: "New booking: {{1}} ({{2}}) for {{3}} on {{4}} at {{5}}.
           Source: outbound follow-up call."
    Params (in order): client_name, client_phone, topic, date, time

Names are configurable per-assistant so multiple verticals (tax, real estate,
clinic) can each have their own approved templates.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from bson import ObjectId

from app.config.database import Database

logger = logging.getLogger(__name__)


# ── Outcome audit ────────────────────────────────────────────────────────────
# Outcomes are stamped onto the existing `call_logs` document for the call.
# A separate collection felt like overkill — the call_log is already the
# canonical per-call record and the dashboard already queries it. Field name
# is prefixed `followup_*` so it never collides with other call_log fields.
_OUTCOME_FILED = "filed"
_OUTCOME_BOOKED = "booked"
_OUTCOME_DECLINED = "declined"
_OUTCOME_NO_ANSWER = "no_answer"  # written by post-call, not from in-call tools

VALID_OUTCOMES = {_OUTCOME_FILED, _OUTCOME_BOOKED, _OUTCOME_DECLINED, _OUTCOME_NO_ANSWER}


def _stamp_outcome_sync(
    *,
    livekit_room: Optional[str],
    call_sid: Optional[str],
    assistant_id: Optional[str],
    outcome: str,
    extra: Dict[str, Any],
) -> bool:
    """Find the call_log for this room/CallSid and stamp the followup outcome.

    Sync Mongo by design — callers run this off the event loop via
    `asyncio.to_thread` so the audio pipeline doesn't stall.

    Lookup priority: call_sid > livekit_room. (call_sid is unique across all
    Twilio calls; livekit_room is unique per room but can be reused on a
    test/dev environment.) Falls back to upsert by room if no log exists yet
    — for outbound calls the log is normally created before the agent joins,
    but on a brand-new test bench it might not be.
    """
    if outcome not in VALID_OUTCOMES:
        logger.warning("[FOLLOWUP] refusing to stamp unknown outcome=%r", outcome)
        return False

    try:
        db = Database.get_db()
        call_logs = db["call_logs"]

        query: Dict[str, Any] = {}
        if call_sid:
            query["call_sid"] = call_sid
        elif livekit_room:
            query["livekit_room"] = livekit_room
        else:
            logger.warning("[FOLLOWUP] cannot stamp outcome: no call_sid/livekit_room")
            return False

        update_doc = {
            "followup_outcome": outcome,
            "followup_outcome_at": datetime.utcnow(),
            "followup_extra": extra or {},
        }
        if assistant_id:
            update_doc["assistant_id"] = ObjectId(assistant_id) if ObjectId.is_valid(assistant_id) else assistant_id

        res = call_logs.update_one(query, {"$set": update_doc})
        if res.matched_count == 0:
            logger.info(
                "[FOLLOWUP] no call_log matched %s; outcome=%s recorded as orphan",
                query, outcome,
            )
            # Orphan: record into a side collection so the outcome isn't lost.
            # This is rare — only happens on dev benches where the call_log
            # wasn't created.
            db["followup_orphan_outcomes"].insert_one({
                **query,
                "assistant_id": assistant_id,
                "outcome": outcome,
                "extra": extra,
                "created_at": datetime.utcnow(),
            })
            return False
        return True
    except Exception:
        logger.exception("[FOLLOWUP] stamp_outcome failed for outcome=%s", outcome)
        return False


# ── Public API: filing-status recording ──────────────────────────────────────
async def record_filing_status(
    *,
    filed: bool,
    notes: str,
    livekit_room: Optional[str],
    call_sid: Optional[str],
    assistant_id: Optional[str],
) -> Dict[str, Any]:
    """Stamp whether the client confirmed they filed.

    `filed=True`  → outcome=filed   (no further action; the bot wraps up)
    `filed=False` → outcome=declined (placeholder — flips to `booked` if the
                                       client agrees to an appointment next)

    Returns a short dict the agent's @function_tool wrapper can stringify
    back to the LLM. Keeping payloads tiny keeps OpenAI tool-call round-trips
    fast (every extra token in the tool result is a token in the next LLM
    request).
    """
    import asyncio
    outcome = _OUTCOME_FILED if filed else _OUTCOME_DECLINED
    extra = {"notes": (notes or "").strip()[:500]}
    ok = await asyncio.to_thread(
        _stamp_outcome_sync,
        livekit_room=livekit_room,
        call_sid=call_sid,
        assistant_id=assistant_id,
        outcome=outcome,
        extra=extra,
    )
    return {"ok": ok, "outcome": outcome}


# ── Public API: booking + WhatsApp confirmation ──────────────────────────────
async def book_appointment_and_notify(
    *,
    user_id: str,
    assistant_id: str,
    livekit_room: Optional[str],
    call_sid: Optional[str],
    client_phone: str,
    client_name: str,
    start_iso: str,
    duration_minutes: int,
    timezone_str: str,
    # Assistant-level config (pulled from self._config in the caller):
    ca_name: str,
    ca_phone: str,
    ca_calendar_account_id: str,
    firm_name: str,
    wa_template_client: str,
    wa_template_ca: str,
    topic: str = "tax filing",
) -> Dict[str, Any]:
    """Write a real calendar event for the CA and fire WhatsApp confirms to
    both the client and the CA. Idempotent-ish: caller is expected to gate on
    the agent saying "confirmed?" first; this function does NOT prompt or
    re-ask.

    Returns a dict shaped for the agent function-tool wrapper:
        { "ok": bool, "event_id": str|None, "wa_client": bool,
          "wa_ca": bool, "error": str|None }

    Failure modes are partial-success-tolerant:
      • calendar succeeds but WhatsApp fails → still considered booked
        (the event exists; ops can retry the WA send post-call).
      • calendar fails → ok=False, no WA sent (don't promise a slot we
        couldn't write).
    """
    from app.services.calendar_service import CalendarService
    from app.services.appointment_whatsapp_service import AppointmentWhatsAppService

    result: Dict[str, Any] = {
        "ok": False,
        "event_id": None,
        "wa_client": False,
        "wa_ca": False,
        "error": None,
    }

    # 1. Validate the slot. Parse the ISO once; if the LLM produced gibberish
    #    we bail out BEFORE touching Google — much friendlier failure mode
    #    (the agent can re-ask) than a 400 from the calendar API.
    try:
        start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except Exception:
        result["error"] = "invalid_start_iso"
        logger.warning("[FOLLOWUP] book: invalid start_iso=%r", start_iso)
        return result

    duration = max(5, min(int(duration_minutes or 30), 240))
    end_dt = start_dt + timedelta(minutes=duration)

    # 2. Create the calendar event.
    #    We use `book_appointment` with `calendar_account_id_override` so we
    #    write to the CA's calendar specifically (not the campaign-default).
    #    A synthetic campaign_id is required by the existing signature; we
    #    accept the lookup miss and fall through to the override path (it
    #    early-returns calendar_account from the override before touching
    #    the campaign — see calendar_service.book_appointment line ~496).
    try:
        cal = CalendarService()
        appointment_data = {
            "title": f"{topic.title()} consultation: {client_name}",
            "start_iso": start_dt.isoformat(),
            "end_iso": end_dt.isoformat(),
            "timezone": timezone_str,
            "duration_minutes": duration,
            "customer_name": client_name,
            "customer_phone": client_phone,
            "notes": (
                f"Outbound follow-up call.\n"
                f"Client: {client_name} ({client_phone})\n"
                f"Topic: {topic}\n"
                f"Booked via Convis voice agent (room={livekit_room or '-'})."
            ),
        }
        # NB: book_appointment was written for campaign-driven flows. For the
        # outbound-followup case we don't always have a real campaign doc,
        # so we route through book_inbound_appointment which only needs
        # user_id + assistant_id + the calendar override. (Calendar-side it
        # produces the same Google event; only the appointments-collection
        # doc differs slightly.)
        event_id = await cal.book_inbound_appointment(
            call_sid=call_sid or (livekit_room or "outbound-followup"),
            user_id=user_id,
            assistant_id=assistant_id,
            appointment=appointment_data,
            calendar_account_id=ca_calendar_account_id,
        )
        if not event_id:
            result["error"] = "calendar_create_failed"
            logger.warning("[FOLLOWUP] calendar event creation returned None")
            return result
        result["event_id"] = event_id
        result["ok"] = True
    except Exception as e:
        logger.exception("[FOLLOWUP] calendar create raised")
        result["error"] = f"calendar_exception: {type(e).__name__}"
        return result

    # 3. Fire both WhatsApp templates. Single per-user WhatsAppService — both
    #    messages share the user's approved templates (the user/business is
    #    the SENDER for both). Independent send_template_message calls so a
    #    failure on one doesn't block the other.
    try:
        wa = await AppointmentWhatsAppService.get_whatsapp_service_for_user(user_id)
    except Exception:
        logger.exception("[FOLLOWUP] failed to construct WhatsApp service")
        wa = None

    if wa is None:
        logger.warning(
            "[FOLLOWUP] no active WhatsApp credential for user %s; "
            "appointment booked but confirmations skipped",
            user_id,
        )
        return result

    # Format date/time once — same strings to both templates so the client
    # and CA see consistent timing.
    formatted_date = start_dt.strftime("%B %d, %Y")
    formatted_time = start_dt.strftime("%I:%M %p")

    # Client template: Hi {client_name}, this is {firm_name}. Your tax
    # consultation with {ca_name} is confirmed for {date} at {time}...
    if wa_template_client and client_phone:
        try:
            r = await wa.send_template_message(
                to=client_phone,
                template_name=wa_template_client,
                parameters=[client_name, firm_name, ca_name, formatted_date, formatted_time],
            )
            result["wa_client"] = bool(r.get("success"))
            if not r.get("success"):
                logger.warning(
                    "[FOLLOWUP] WA client template send failed: %s",
                    r.get("error"),
                )
        except Exception:
            logger.exception("[FOLLOWUP] WA client send raised")

    # CA template: New booking: {client_name} ({client_phone}) for {topic} on
    # {date} at {time}.
    if wa_template_ca and ca_phone:
        try:
            r = await wa.send_template_message(
                to=ca_phone,
                template_name=wa_template_ca,
                parameters=[client_name, client_phone, topic, formatted_date, formatted_time],
            )
            result["wa_ca"] = bool(r.get("success"))
            if not r.get("success"):
                logger.warning(
                    "[FOLLOWUP] WA CA template send failed: %s",
                    r.get("error"),
                )
        except Exception:
            logger.exception("[FOLLOWUP] WA CA send raised")

    # 4. Stamp the call_log outcome=booked with the slot info so the dashboard
    #    can show "X of Y outbound calls converted to a booking".
    import asyncio
    await asyncio.to_thread(
        _stamp_outcome_sync,
        livekit_room=livekit_room,
        call_sid=call_sid,
        assistant_id=assistant_id,
        outcome=_OUTCOME_BOOKED,
        extra={
            "event_id": result["event_id"],
            "ca_calendar_account_id": ca_calendar_account_id,
            "start_iso": start_dt.isoformat(),
            "duration_minutes": duration,
            "wa_client": result["wa_client"],
            "wa_ca": result["wa_ca"],
        },
    )

    return result


# ── Helpers for the @function_tool wrappers in agent_worker ──────────────────
def is_followup_effective(config: Dict[str, Any]) -> bool:
    """Gate used by both function-tool wrappers AND the prompt-suffix builder.

    All four are required for the workflow to actually function:
      • feature flag on
      • CA calendar id (no point booking with no calendar to write to)
      • CA phone (no point firing the CA template with no recipient)
      • at least one WhatsApp template name (client or CA)

    Missing any of these → wrappers no-op and the LLM is never told about
    the tools (the suffix isn't appended). Better to refuse than to half-do.
    """
    if not bool(config.get("outbound_followup_enabled")):
        return False
    if not (config.get("ca_calendar_account_id") or "").strip():
        return False
    if not (config.get("ca_phone") or "").strip():
        return False
    has_template = bool(
        (config.get("wa_template_client") or "").strip()
        or (config.get("wa_template_ca") or "").strip()
    )
    return has_template
