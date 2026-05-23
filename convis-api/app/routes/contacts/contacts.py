"""Contacts API — manage the per-tenant durable identities behind the
conversation-memory feature, and the per-contact right-to-be-forgotten
opt-out.

Why this router exists (QA finding — it was a Blocker):
    `contact_service.set_do_not_remember` — the entire RTBF mechanism that
    the feature's docstrings repeatedly promise — had NO caller. The flag
    `contacts.do_not_remember` could only ever be `False` in production:
    `get_or_create_contact` hard-codes it `False` and nothing flipped it.
    The opt-out checks in `build_context_block` / `extract_and_persist_summary`
    were testing a flag nothing could set. This router wires it up.

Endpoints (all JWT-authenticated, all strictly tenant-scoped):
    GET    /api/contacts                — list this tenant's contacts
    GET    /api/contacts/{contact_id}   — one contact
    PATCH  /api/contacts/{contact_id}   — edit name/metadata, flip do_not_remember
    DELETE /api/contacts/{contact_id}   — hard-delete the contact + its summaries

Tenant isolation: every query filters on `user_id == current_user`. A
contact belonging to another tenant is reported as 404 (not 403) so the
endpoint can't be used to probe which contact ids exist.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.config.database import Database
from app.models.contact import ContactResponse, ContactUpdate
from app.services.contact_service import set_do_not_remember
from app.utils.auth import get_current_user

logger = logging.getLogger(__name__)

# Every endpoint here requires a valid JWT. Contact phone numbers + names are
# PII — anonymous access would be a Blocker.
router = APIRouter(dependencies=[Depends(get_current_user)])

# Free-form metadata is meant to stay small (see contact.py docstring). Reject
# anything that would bloat the doc — large blobs belong on the call_log.
_MAX_METADATA_BYTES = 1024


def _summary_stats(db, contact_ids: List[ObjectId], user_oid: ObjectId) -> Dict[ObjectId, Dict[str, Any]]:
    """One aggregation (NOT N+1) returning {contact_id: {count, last_date}}
    for the given page of contacts. Tenant-scoped on user_id."""
    if not contact_ids:
        return {}
    try:
        rows = db["call_summaries"].aggregate([
            {"$match": {"contact_id": {"$in": contact_ids}, "user_id": user_oid}},
            {"$group": {
                "_id": "$contact_id",
                "count": {"$sum": 1},
                "last_date": {"$max": "$date"},
            }},
        ])
        return {r["_id"]: {"count": r["count"], "last_date": r.get("last_date")} for r in rows}
    except Exception:
        logger.exception("[CONTACTS] summary-stats aggregation failed")
        return {}


def _to_response(contact: Dict[str, Any], stats: Optional[Dict[str, Any]]) -> ContactResponse:
    """Map a Mongo contact doc → API response shape."""
    return ContactResponse(
        id=str(contact["_id"]),
        user_id=str(contact.get("user_id", "")),
        phone_number=contact.get("phone_number", ""),
        name=contact.get("name"),
        metadata=contact.get("metadata") or {},
        do_not_remember=bool(contact.get("do_not_remember", False)),
        created_at=contact.get("created_at") or datetime.now(timezone.utc),
        updated_at=contact.get("updated_at") or datetime.now(timezone.utc),
        call_count=(stats or {}).get("count"),
        last_call_at=(stats or {}).get("last_date"),
    )


def _current_user_oid(current_user: dict) -> ObjectId:
    """The JWT path already validated this is a real ObjectId — but coerce
    defensively rather than trust the shape."""
    uid = current_user.get("user_id")
    if not uid or not ObjectId.is_valid(uid):
        # Should be unreachable (get_current_user guarantees it) — 401 not 500.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid user identity")
    return ObjectId(uid)


@router.get("", response_model=Dict[str, Any])
async def list_contacts(
    current_user: dict = Depends(get_current_user),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    phone: Optional[str] = Query(None, description="Optional exact phone filter"),
):
    """List the current tenant's contacts, newest-activity first, with a
    call_count + last_call_at per contact (one aggregation, not N+1)."""
    db = Database.get_db()
    user_oid = _current_user_oid(current_user)

    query: Dict[str, Any] = {"user_id": user_oid}
    if phone:
        query["phone_number"] = phone.strip()

    total = db["contacts"].count_documents(query)
    contacts = list(
        db["contacts"].find(query).sort("updated_at", -1).skip(skip).limit(limit)
    )
    stats = _summary_stats(db, [c["_id"] for c in contacts], user_oid)

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "contacts": [
            _to_response(c, stats.get(c["_id"])).model_dump() for c in contacts
        ],
    }


@router.get("/{contact_id}", response_model=ContactResponse)
async def get_contact(contact_id: str, current_user: dict = Depends(get_current_user)):
    """Fetch one contact. 404 (not 403) if it belongs to another tenant —
    don't leak existence of other tenants' contact ids."""
    if not ObjectId.is_valid(contact_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid contact id")
    db = Database.get_db()
    user_oid = _current_user_oid(current_user)
    cid = ObjectId(contact_id)

    contact = db["contacts"].find_one({"_id": cid, "user_id": user_oid})
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")

    stats = _summary_stats(db, [cid], user_oid).get(cid)
    return _to_response(contact, stats)


@router.patch("/{contact_id}", response_model=ContactResponse)
async def update_contact(
    contact_id: str,
    body: ContactUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Edit a contact. The headline capability: flipping `do_not_remember`.

    `do_not_remember=True` is right-to-be-forgotten — it routes through
    `contact_service.set_do_not_remember`, which (tenant-scoped) sets the
    flag AND cascade-deletes every existing call_summary for the contact.
    `name` / `metadata` are updated directly.
    """
    if not ObjectId.is_valid(contact_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid contact id")
    db = Database.get_db()
    user_oid = _current_user_oid(current_user)
    cid = ObjectId(contact_id)

    # Tenant-scoped existence check up front — 404 for anyone else's contact.
    contact = db["contacts"].find_one({"_id": cid, "user_id": user_oid})
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")

    # 1. name / metadata — direct field updates.
    set_fields: Dict[str, Any] = {}
    if body.name is not None:
        set_fields["name"] = body.name.strip()[:200] or None
    if body.metadata is not None:
        try:
            if len(json.dumps(body.metadata).encode("utf-8")) > _MAX_METADATA_BYTES:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"metadata exceeds {_MAX_METADATA_BYTES} bytes",
                )
        except (TypeError, ValueError):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "metadata is not JSON-serializable")
        set_fields["metadata"] = body.metadata
    if set_fields:
        set_fields["updated_at"] = datetime.now(timezone.utc)
        db["contacts"].update_one({"_id": cid, "user_id": user_oid}, {"$set": set_fields})

    # 2. do_not_remember — through the service (handles the cascade delete +
    # its own belt-and-suspenders tenant filter). Run it whenever the field
    # is supplied: an ON→ON re-flip harmlessly re-runs the cascade, which is
    # actually useful for sweeping up any summary that slipped in via the
    # extraction/opt-out race.
    if body.do_not_remember is not None:
        res = await set_do_not_remember(
            contact_id=contact_id,
            user_id=current_user["user_id"],
            do_not_remember=bool(body.do_not_remember),
        )
        if not res.get("ok"):
            err = res.get("error", "unknown")
            # contact_not_found_or_wrong_tenant shouldn't happen (we checked
            # above) but map it to 404 to stay consistent; everything else 500.
            code = (
                status.HTTP_404_NOT_FOUND
                if err == "contact_not_found_or_wrong_tenant"
                else status.HTTP_500_INTERNAL_SERVER_ERROR
            )
            raise HTTPException(code, f"do_not_remember update failed: {err}")
        if body.do_not_remember:
            logger.info(
                "[CONTACTS] contact=%s opted out (do_not_remember=ON); "
                "cascade-deleted %d summaries",
                cid, res.get("deleted_summaries", 0),
            )

    updated = db["contacts"].find_one({"_id": cid, "user_id": user_oid})
    stats = _summary_stats(db, [cid], user_oid).get(cid)
    return _to_response(updated or contact, stats)


@router.delete("/{contact_id}", response_model=Dict[str, Any])
async def delete_contact(contact_id: str, current_user: dict = Depends(get_current_user)):
    """Hard-delete a contact and ALL its call_summaries (full erasure).

    Distinct from `do_not_remember`: that keeps the contact row (so future
    calls don't keep re-creating it) but forgets the history; this removes
    the contact entirely. Both are tenant-scoped."""
    if not ObjectId.is_valid(contact_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid contact id")
    db = Database.get_db()
    user_oid = _current_user_oid(current_user)
    cid = ObjectId(contact_id)

    contact = db["contacts"].find_one({"_id": cid, "user_id": user_oid})
    if not contact:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")

    # Summaries first so a crash mid-delete can't orphan them under a missing
    # contact. Both filters carry user_id — never a cross-tenant delete.
    del_summaries = db["call_summaries"].delete_many({"contact_id": cid, "user_id": user_oid})
    db["contacts"].delete_one({"_id": cid, "user_id": user_oid})
    logger.info(
        "[CONTACTS] hard-deleted contact=%s + %d summaries",
        cid, del_summaries.deleted_count,
    )
    return {"ok": True, "deleted_contact": contact_id, "deleted_summaries": del_summaries.deleted_count}
