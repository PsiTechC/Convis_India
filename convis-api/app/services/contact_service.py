"""Contact service — durable per-tenant identity for someone the bot
talks to across multiple calls.

The single function in here that the rest of the platform cares about is
`get_or_create_contact(user_id, phone_number, name_hint=None)`. Two paths
fire it concurrently:

  • The post-call summary extractor (after every completed call where the
    assistant has conversation_history_enabled) — to find or create the
    Contact that the new CallSummary will be attached to.

  • The pre-call context-injection step in load_assistant_config — to
    look up which Contact this call is FOR, so the prior summaries can
    be fetched.

Both paths can race against each other (and against themselves on Twilio
webhook retries). Idempotency rests on the unique Mongo index
`{user_id, phone_number}` declared in contact.py. We do an upsert keyed
on that pair; the second concurrent writer hits the unique index and
falls back to the existing document.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.config.database import Database

logger = logging.getLogger(__name__)


# E.164 — the same regex used elsewhere in the platform (assistant_config.py
# call_transfer numbers, twilio_signature, etc.). Inline rather than
# imported to keep this service free of livekit-stack imports.
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


def _normalize_phone(raw: Any) -> Optional[str]:
    """Strip + canonicalize. Returns None if the input doesn't look like a
    phone number we can persist — caller must handle (don't create orphan
    contacts with junk numbers).

    Requires explicit leading `+` (country code). A 10-digit US number
    without `+1` is ambiguous (could be Switzerland +4...) — we reject
    those rather than guess. Upstream (CSV upload, Twilio webhook) is
    responsible for producing properly-formatted E.164.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not s or not s.startswith("+"):
        return None
    if not _E164_RE.fullmatch(s):
        return None
    return s


def resolve_contact_phone(
    *,
    direction: Any,
    from_number: Any,
    to_number: Any,
    customer_phone: Any = None,
) -> Optional[Any]:
    """Single source of truth for "which phone number identifies the contact
    on this call".

    This MUST be used identically by BOTH:
      • the pre-call read path  (agent_worker → build_context_block), and
      • the post-call write path (post_call_summary_service →
        extract_and_persist_summary).

    If the two sides disagree on the number, the summary is written under one
    contact and looked up under another — and conversation memory silently
    no-ops with no error. (This was a real QA finding: the read path preferred
    `customer_phone` first while the write path preferred `from_number`/
    `to_number` first, and the inbound write path ignored `customer_phone`
    entirely.)

    Rule:
      • INBOUND  → the contact is the caller  → `from_number`.
      • OUTBOUND → the contact is the callee  → `to_number`.
      • `customer_phone` is the explicit fallback in BOTH directions, used
        only when the direction-primary field is absent.

    Returns the raw (un-normalized) value; callers pass it through
    `_normalize_phone` / `get_or_create_contact`.
    """
    primary = from_number if (str(direction or "").lower() == "inbound") else to_number
    return primary or customer_phone or None


def _coerce_user_id(user_id: Any) -> Optional[ObjectId]:
    """Tolerate string-or-ObjectId. Returns None for invalid input so the
    caller can short-circuit cleanly rather than 500'ing on a bad upstream."""
    if isinstance(user_id, ObjectId):
        return user_id
    if isinstance(user_id, str) and ObjectId.is_valid(user_id):
        return ObjectId(user_id)
    return None


def _get_or_create_contact_sync(
    *,
    user_id: ObjectId,
    phone_e164: str,
    name_hint: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Idempotent upsert. The actual sync Mongo work — wrap in
    asyncio.to_thread when called from async contexts.

    Returns the contact document (with stringified _id-friendly shape) or
    None on Mongo error. Honours an existing `do_not_remember=True` flag
    by leaving it alone — caller must check the flag and act accordingly.

    Name update policy: only fill `name` from `name_hint` if the existing
    contact has no name OR has an obvious placeholder ("", "Customer",
    "Unknown"). Never overwrite a real-looking name that an operator
    may have edited.
    """
    try:
        db = Database.get_db()
        contacts = db["contacts"]
        now = datetime.now(timezone.utc)

        # Look up first — common path, avoids the upsert race entirely
        # most of the time.
        existing = contacts.find_one({"user_id": user_id, "phone_number": phone_e164})
        if existing:
            # Optional name fill-in. Only when the contact has a placeholder
            # name AND we have a real hint to use — never overwrite a real
            # name (operator may have edited it).
            existing_name = (existing.get("name") or "").strip()
            # Case-insensitive + a wider placeholder set — an operator's CSV
            # or a carrier CNAM lookup produces these in any casing.
            is_placeholder = existing_name.lower() in (
                "", "customer", "unknown", "unknown caller", "guest",
                "caller", "n/a", "none", "no name", "anonymous",
            )
            new_hint = (name_hint or "").strip()[:200]
            if new_hint and is_placeholder:
                contacts.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {"name": new_hint, "updated_at": now}},
                )
                existing["name"] = new_hint
                existing["updated_at"] = now
            return existing

        # Create. The unique index protects us against a concurrent insert
        # racing in between the find_one and the insert; if that happens,
        # DuplicateKeyError fires and we re-fetch.
        doc = {
            "user_id": user_id,
            "phone_number": phone_e164,
            "name": (name_hint or "").strip()[:200] or None,
            "metadata": {},
            "do_not_remember": False,
            "created_at": now,
            "updated_at": now,
        }
        try:
            ins = contacts.insert_one(doc)
            doc["_id"] = ins.inserted_id
            return doc
        except DuplicateKeyError:
            # The other racing writer already inserted — re-fetch and return.
            return contacts.find_one({"user_id": user_id, "phone_number": phone_e164})

    except Exception:
        logger.exception(
            "[CONTACT] get_or_create failed for user=%s phone=%s", user_id, phone_e164,
        )
        return None


async def get_or_create_contact(
    *,
    user_id: Any,
    phone_number: Any,
    name_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Async wrapper. The hot path is post-call-webhook → run summary →
    upsert contact → save summary; livekit's agent process is async so we
    must not block the event loop with sync pymongo. Same pattern as
    `_mark_call_completed` and `_lookup_twilio_call_sid` in agent_worker.

    Returns:
        The contact dict (Mongo doc shape with ObjectId _id), or None if
        the user_id / phone_number couldn't be normalized OR Mongo failed.
        Callers MUST handle None — don't crash a real call because we
        couldn't persist a contact.
    """
    uid = _coerce_user_id(user_id)
    if uid is None:
        logger.warning("[CONTACT] invalid user_id=%r — refusing to create contact", user_id)
        return None
    phone = _normalize_phone(phone_number)
    if phone is None:
        logger.warning(
            "[CONTACT] invalid phone_number=%r for user=%s — refusing to create contact",
            phone_number, uid,
        )
        return None

    return await asyncio.to_thread(
        _get_or_create_contact_sync,
        user_id=uid,
        phone_e164=phone,
        name_hint=name_hint,
    )


async def set_do_not_remember(
    *,
    contact_id: Any,
    user_id: Any,
    do_not_remember: bool,
) -> Dict[str, Any]:
    """Flip a contact's do_not_remember flag and, if turning ON, cascade-
    delete all existing call_summaries for that contact (right-to-be-
    forgotten compliance).

    Returns {ok: bool, deleted_summaries: int, error?: str}.

    DESIGN: the cascade is asymmetric on purpose.
      • OFF → ON: delete all existing summaries. The flag's semantic is "do
        not act on this person's history" — if we left the summaries, an
        operator (or a future bug) could re-enable and the data would
        resurface. Delete = irreversible commitment that matches the
        user-facing language.
      • ON → OFF: do nothing. Summaries are already gone from the previous
        flip. Future calls start producing summaries again.

    Tenant-scoped: requires both contact_id AND user_id to match — a
    contact's owner is the only one allowed to flip this flag. Caller MUST
    pre-verify the caller's auth identity matches `user_id` before invoking
    this function (the route handler does that — this helper trusts its
    inputs and just enforces tenant scope to prevent cross-tenant cascade).
    """
    uid = _coerce_user_id(user_id)
    if uid is None:
        return {"ok": False, "deleted_summaries": 0, "error": "invalid_user_id"}
    if not ObjectId.is_valid(contact_id):
        return {"ok": False, "deleted_summaries": 0, "error": "invalid_contact_id"}
    cid = ObjectId(contact_id) if isinstance(contact_id, str) else contact_id

    def _do() -> Dict[str, Any]:
        try:
            db = Database.get_db()
            now = datetime.now(timezone.utc)
            res = db["contacts"].update_one(
                {"_id": cid, "user_id": uid},
                {"$set": {"do_not_remember": bool(do_not_remember), "updated_at": now}},
            )
            if res.matched_count == 0:
                return {"ok": False, "deleted_summaries": 0, "error": "contact_not_found_or_wrong_tenant"}

            deleted = 0
            if do_not_remember:
                # Cascade: delete all call_summaries linked to this contact.
                # Use `user_id` in the filter as a belt-and-suspenders tenant
                # check — even though contact_id is unique, double-filtering
                # prevents cross-tenant deletion if a malformed callsite ever
                # passes a wrong (user_id, contact_id) pair.
                del_res = db["call_summaries"].delete_many(
                    {"contact_id": cid, "user_id": uid},
                )
                deleted = int(del_res.deleted_count)
                logger.info(
                    "[CONTACT] do_not_remember ON for contact=%s; deleted %d summaries",
                    cid, deleted,
                )
            else:
                logger.info("[CONTACT] do_not_remember OFF for contact=%s", cid)

            return {"ok": True, "deleted_summaries": deleted}
        except Exception as exc:
            logger.exception("[CONTACT] set_do_not_remember failed for contact=%s", cid)
            return {"ok": False, "deleted_summaries": 0, "error": f"exception: {type(exc).__name__}"}

    return await asyncio.to_thread(_do)


async def get_contact_by_phone(
    *,
    user_id: Any,
    phone_number: Any,
) -> Optional[Dict[str, Any]]:
    """Read-only lookup for the pre-call context-injection path. Doesn't
    create — if no contact exists, there's no history to inject and we
    return None (the bot just starts cold, which is correct first-call
    behaviour anyway)."""
    uid = _coerce_user_id(user_id)
    if uid is None:
        return None
    phone = _normalize_phone(phone_number)
    if phone is None:
        return None

    def _lookup() -> Optional[Dict[str, Any]]:
        try:
            return Database.get_db()["contacts"].find_one(
                {"user_id": uid, "phone_number": phone},
            )
        except Exception:
            logger.exception("[CONTACT] lookup failed for user=%s phone=%s", uid, phone)
            return None

    return await asyncio.to_thread(_lookup)
