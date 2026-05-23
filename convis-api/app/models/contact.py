"""Contact model — per-tenant persistent identity for someone the bot
talks to across multiple calls.

A Contact is the durable identity that lets us thread conversation history
across separate phone calls (the "remember what they said last time" feature
on assistants with `conversation_history_enabled=True`). One Contact corresponds
to one phone number within one tenant (user_id); each call against that number
produces a CallSummary linked to the Contact.

Why this lives alongside `leads` rather than replacing it:
  • Leads are campaign-scoped, transient, and may belong to many campaigns.
  • Contacts are tenant-scoped, durable, and aggregate across all
    interactions — campaigns, ad-hoc outbound, AND inbound calls.
  • A lead's name/phone might be wrong; the Contact's name is corrected
    over time as the bot learns it during real calls.

Privacy notes:
  • `do_not_remember=True` is the per-contact opt-out flag — when set, the
    post-call summary extraction is SKIPPED for this contact, and no
    history is ever injected for them. Compliance with right-to-be-forgotten.
  • Free-form `metadata` stays small (<1KB) — anything larger goes on the
    underlying call_log or call_summary, not here.
"""
from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ContactBase(BaseModel):
    """Shared base for Contact API shapes."""
    phone_number: str = Field(..., description="E.164, +CCNNNN…")
    name: Optional[str] = Field(default=None, max_length=200)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    do_not_remember: bool = Field(
        default=False,
        description=(
            "When True, post-call summaries are skipped and no history is "
            "injected on subsequent calls. Honour the contact's request to "
            "be forgotten without losing the audit trail of the call itself."
        ),
    )


class ContactCreate(ContactBase):
    """Payload shape for explicit contact creation via API (not the upsert
    path used by post-call summary extraction)."""
    pass


class ContactUpdate(BaseModel):
    """Optional-everything update. Existing values preserved on omitted fields."""
    name: Optional[str] = Field(default=None, max_length=200)
    metadata: Optional[Dict[str, Any]] = None
    do_not_remember: Optional[bool] = None


class ContactResponse(ContactBase):
    """API response shape (Mongo _id stringified)."""
    id: str = Field(..., description="Mongo _id as string")
    user_id: str
    created_at: datetime
    updated_at: datetime
    # Convenience fields for the dashboard list view — not persisted, filled
    # in by the route handler.
    call_count: Optional[int] = None
    last_call_at: Optional[datetime] = None


# ── Mongo storage shape (not a Pydantic model — Mongo dict for clarity) ──────
#
# Collection: contacts
# {
#   _id:                  ObjectId,
#   user_id:              ObjectId,              # tenant
#   phone_number:         "+14155550123",        # E.164; unique per user_id
#   name:                 "Alice Patel",
#   metadata:             {},
#   do_not_remember:      false,
#   created_at:           ISODate,
#   updated_at:           ISODate,
# }
#
# REQUIRED INDEXES (create once via a Mongo init script — this repo
# documents indexes in module docstrings rather than enforcing them in code,
# matching the pattern used for `assistants`, `call_logs`, etc.):
#
#   db.contacts.createIndex(
#       {user_id: 1, phone_number: 1},
#       {unique: true, name: "uniq_user_phone"},
#   )
#   db.contacts.createIndex({user_id: 1, updated_at: -1}, {name: "recent_per_user"})
#
# The unique index is load-bearing: it prevents two concurrent webhook
# handlers from creating duplicate contacts for the same (tenant, phone).
# The upsert path in contact_service relies on it for idempotency.
