"""CallSummary model — LLM-extracted structured summary of a single call,
keyed to the durable Contact identity.

One CallSummary per completed call (where the assistant has
`conversation_history_enabled=True`). The summary is what gets injected
into the system prompt on the contact's NEXT call, so the bot remembers
what was discussed last time.

Extraction is async (post-call, after Twilio recording + transcription
are available). The full transcript stays on `call_logs.transcription`;
this collection holds the compressed structured form that's cheap to
fetch on the next-call hot path.

Schema is deliberately VERTICAL-AGNOSTIC (no healthcare-specific fields
like `symptoms` or `mood`). The same shape serves tax / legal / sales
follow-ups equally — operators write the system prompt that determines
what the bot asks; the extractor just records what was said in
generic, factual terms.

The `extraction_version` field is stamped on every summary for forward
compatibility — when the extraction prompt or schema materially changes,
bump the EXTRACTION_VERSION constant so consumers can detect drift.
NOTE: there is currently NO automatic re-extraction of old summaries —
the backfill loop only fills in call_logs that have NO summary at all
(`summary: {$size: 0}`), it does not re-process version-N summaries to
version-N+1. A version bump only affects summaries written after it.
Re-extracting old summaries would need a dedicated one-off admin job.
"""
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# Bump this when the extraction prompt or schema materially changes.
# Stored on each summary so consumers can detect schema drift and trigger
# a re-extract job if needed.
EXTRACTION_VERSION = 1


Sentiment = Literal["positive", "neutral", "negative"]


class CallSummarySchema(BaseModel):
    """The JSON shape returned by the extraction LLM. This is what we ask
    OpenAI to produce in `response_format={"type": "json_object"}` mode;
    pydantic validates the response before we trust it into Mongo.

    Generic across verticals — `key_facts` holds the domain-specific
    detail (BP readings, filing year, deal size, …) without us having to
    extend the schema per use case.
    """
    key_facts: List[str] = Field(
        default_factory=list,
        description=(
            "Factual claims the caller stated, in their own words, as short "
            "fragments. E.g. ['filed last year', 'BP 140/90', 'lives in Bangalore']."
        ),
    )
    outcomes: List[str] = Field(
        default_factory=list,
        description=(
            "What got decided or agreed during the call. E.g. ['booked May 22 11am', "
            "'asked for callback Tuesday', 'declined appointment']."
        ),
    )
    follow_up_items: List[str] = Field(
        default_factory=list,
        description=(
            "Open items the next caller should revisit. E.g. ['ask about "
            "dizziness', 'confirm document upload']."
        ),
    )
    sentiment: Sentiment = Field(
        default="neutral",
        description="Caller's emotional tone, three buckets only.",
    )
    raw_summary: str = Field(
        default="",
        max_length=2000,
        description="3-4 sentence prose summary in past tense.",
    )


class CallSummaryResponse(BaseModel):
    """API response shape (Mongo _id and ObjectId fields stringified)."""
    id: str
    contact_id: str
    call_log_id: str
    user_id: str
    assistant_id: Optional[str] = None
    date: datetime
    # Inlined schema fields
    key_facts: List[str]
    outcomes: List[str]
    follow_up_items: List[str]
    sentiment: Sentiment
    raw_summary: str
    # Audit
    extraction_model: str
    extraction_version: int
    created_at: datetime


# ── Mongo storage shape (documented; this repo doesn't enforce schema) ───────
#
# Collection: call_summaries
# {
#   _id:                  ObjectId,
#   contact_id:           ObjectId,         # FK -> contacts._id
#   call_log_id:          ObjectId,         # FK -> call_logs._id (full transcript)
#   user_id:              ObjectId,         # denormalized for tenant queries
#   assistant_id:         ObjectId,         # which bot produced this
#   date:                 ISODate,          # call_log.created_at; what we sort by
#   key_facts:            ["...", "..."],
#   outcomes:             ["..."],
#   follow_up_items:      ["..."],
#   sentiment:            "neutral",
#   raw_summary:          "Patient said…",
#   extraction_model:     "gpt-4o-mini",
#   extraction_version:   1,
#   created_at:           ISODate,
# }
#
# REQUIRED INDEXES:
#
#   db.call_summaries.createIndex(
#       {contact_id: 1, date: -1},
#       {name: "recent_per_contact"},
#   )
#   db.call_summaries.createIndex(
#       {call_log_id: 1},
#       {unique: true, name: "uniq_call_log_summary"},
#   )
#   db.call_summaries.createIndex({user_id: 1, date: -1}, {name: "recent_per_user"})
#
# `uniq_call_log_summary` is load-bearing: it prevents duplicate summary
# extraction if the post-call webhook fires twice (Twilio retries) or two
# workers race to extract the same call. The extraction service relies on
# `update_one(...upsert=True)` keyed by call_log_id for idempotency.
