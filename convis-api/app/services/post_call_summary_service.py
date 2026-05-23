"""Post-call summary extraction.

For every completed call where the assistant has
`conversation_history_enabled=True`, this service:

  1. Fetches the call_log (transcription + caller identity).
  2. Resolves or creates the Contact for that phone number.
  3. Asks gpt-4o-mini to extract a structured JSON summary
     (key_facts / outcomes / follow_up_items / sentiment / raw_summary).
  4. Persists the summary to `call_summaries`, keyed by call_log_id
     for idempotency.

The pre-call context-injection path (load_assistant_config) reads from
`call_summaries` to build the patient_context_block — so this service
is what makes "the bot remembers what they said last time" actually
work. Without it, the contacts collection fills up but the bot starts
cold every call.

Idempotent by design:
  • Repeating the extraction for the same call_log_id is safe: the
    unique index `{call_log_id: 1}` on call_summaries (see
    call_summary.py) prevents duplicates. The upsert here uses
    `$setOnInsert` for the body so re-runs don't churn updated_at.
  • Honours `contacts.do_not_remember=True` — when the contact has
    opted out, the extraction LLM call is SKIPPED (no PII sent to
    OpenAI, no Mongo write). Caller still gets a clean return so the
    surrounding webhook doesn't think something failed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from pydantic import ValidationError

from app.config.database import Database
from app.models.call_summary import CallSummarySchema, EXTRACTION_VERSION
from app.services.contact_service import get_or_create_contact, resolve_contact_phone

logger = logging.getLogger(__name__)


# Bumping any of these constants is a behaviour change — coordinate with the
# EXTRACTION_VERSION constant in call_summary.py so consumers can detect
# schema/prompt drift on old summaries.
_EXTRACTION_MODEL = "gpt-4o-mini"
# Cap transcript size sent to the LLM. 50K chars ≈ 12K tokens — well under
# gpt-4o-mini's 128K context but bounded so a runaway transcription doesn't
# turn into a $5 extraction. A 3-min call is ~1K chars; even a 30-min call
# is under 10K. The cap protects against an upstream bug filling the
# transcript with junk.
_MAX_TRANSCRIPT_CHARS = 50_000
# Skip extraction below this. Bumped from 30 → 200 after QA: a 30-char
# transcript ("Hello? Yes? Goodbye.") has no extractable content and we'd
# just bill ~$0.0005 for an empty summary. 200 chars ≈ 40 words — a real
# conversation has signal there.
_MIN_TRANSCRIPT_CHARS = 200


# ── Prompt-injection defence ─────────────────────────────────────────────────
# Extracted strings get interpolated into the NEXT call's system prompt. A
# malicious caller can attempt to plant instructions for a future call's
# bot. We sanitize on the write path so persisted summaries are safe to
# inject. The conversation_history_service also wraps the injected block in
# an "untrusted reference data" disclaimer — defence in depth, not relying
# on either layer alone.
#
# Strategy: drop any candidate string that matches a known injection
# pattern. Length-cap each accepted item. The patterns are intentionally
# conservative — false positives (dropping a legitimate caller statement)
# are preferable to a missed injection.
_INJECTION_PATTERNS = re.compile(
    r"(ignore.{0,30}(previous|prior|above|all).{0,30}(instruction|prompt|rule)"
    r"|disregard.{0,30}(previous|prior|above|all).{0,30}(instruction|prompt|rule)"
    r"|forget.{0,30}(previous|prior|above|all).{0,30}(instruction|prompt|rule)"
    # "system prompt:" / "system instruction =" — require a trailing : or =
    # so an ordinary IT-support phrase ("the system prompt error popped up")
    # is NOT flagged, while a real injection header ("System prompt: ...") is.
    r"|system.{0,5}(prompt|instruction)s?\s*[:=]"
    r"|new.{0,20}instructions?:"
    # Role-reassignment. Tightened from a bare "you are a" — which false-
    # matched ordinary speech like "you are a lifesaver" and silently
    # blanked whole positive-sentiment summaries (QA finding). Now requires
    # EITHER "now" OR a persona-noun after "you are/act (as) (a|an)".
    r"|you (are|act)( as)?( now)? (a|an) [^.\n]{0,40}?"
    r"(bot|assistant|\bai\b|model|persona|character|pirate|hacker|genie|chatbot)"
    r"|you are now (a|an|free|unrestricted|jailbroken|in developer)"
    r"|from now on,?\s+you\b"
    r"|</?(system|user|assistant|instruction|s>|im_start|im_end)"
    r"|\[INST\]"
    r"|\bjailbreak\b"
    r")",
    re.IGNORECASE,
)
_MAX_ITEM_CHARS = 300        # any single key_fact / outcome / follow_up
_MAX_RAW_SUMMARY_CHARS = 600  # already requested in the LLM prompt; enforce here


def _sanitize_item(s: Any) -> Optional[str]:
    """Return a safe-to-persist version of one extracted string, or None
    if the input is empty / matches an injection pattern.

    Why this is the right place to filter (not at READ time on the
    injection path): we want bad strings OUT of Mongo entirely, so a
    future viewer / dashboard / re-extract path can't be tricked. Read-
    side filtering would leave the bad data in the DB.
    """
    if not isinstance(s, str):
        return None
    # Collapse ALL interior whitespace (newlines, tabs, runs of spaces) to a
    # single space. Without this, a newline embedded in an extracted fragment
    # survives into the rendered context block and lets a caller forge a fake
    # bullet / header line — structural prompt injection. (QA finding.)
    # Doing it BEFORE the regex check also defeats injections split across
    # newlines to dodge the pattern.
    s = " ".join(s.split())
    if not s:
        return None
    if _INJECTION_PATTERNS.search(s):
        logger.warning(
            "[SUMMARY] dropping likely-injection item: %r", s[:120],
        )
        return None
    return s[:_MAX_ITEM_CHARS]


def _sanitize_list(items: Any) -> List[str]:
    """Apply _sanitize_item across a list; drop Nones; dedupe (case-insensitive)
    so a caller can't waste prompt tokens by repeating one phrase 20 times."""
    if not isinstance(items, list):
        return []
    seen: set = set()
    out: List[str] = []
    for raw in items:
        cleaned = _sanitize_item(raw)
        if cleaned is None:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _sanitize_raw_summary(s: Any) -> str:
    """Sanitize raw_summary (the prose field) separately from list items.

    Difference from _sanitize_item:
      • Larger length cap (600 instead of 300) — this is a paragraph, not
        a fragment.
      • Same injection-regex defence — an attacker could plant the same
        markers in prose form.
      • Returns "" instead of None on drop (the schema field is non-Optional
        and defaults to "").
    """
    if not isinstance(s, str):
        return ""
    # Same whitespace-collapse as _sanitize_item — raw_summary renders onto a
    # single logical line in the context block, and collapsing newlines here
    # blocks structural injection + newline-split evasion of the regex.
    s = " ".join(s.split())
    if not s:
        return ""
    if _INJECTION_PATTERNS.search(s):
        logger.warning(
            "[SUMMARY] dropping likely-injection raw_summary: %r", s[:120],
        )
        return ""
    return s[:_MAX_RAW_SUMMARY_CHARS]


def _sanitize_summary(parsed: CallSummarySchema) -> CallSummarySchema:
    """Return a new CallSummarySchema with all fields sanitized.

    We construct a fresh model instance rather than mutating in place —
    pydantic v2 BaseModels are immutable by default, and creating a new
    one makes the sanitization explicit + auditable.
    """
    return CallSummarySchema(
        key_facts=_sanitize_list(parsed.key_facts),
        outcomes=_sanitize_list(parsed.outcomes),
        follow_up_items=_sanitize_list(parsed.follow_up_items),
        sentiment=parsed.sentiment,
        raw_summary=_sanitize_raw_summary(parsed.raw_summary),
    )


def _build_labeled_transcript(call_log: Dict[str, Any]) -> str:
    """Render the call as a SPEAKER-LABELED transcript for the extractor.

    Prefers `conversation_log` — the role-tagged turn array the post-call
    analyzer produces ([{role: "user"|"assistant", text: ...}, ...]).

    Why this matters (QA finding — the "headache & back pain forever" bug):
    the raw `transcript` field is a flat Deepgram string with NO speaker
    labels. The extraction LLM then cannot tell the CALLER's words from the
    ASSISTANT's — and the assistant OPENS every call by reciting prior-call
    memory ("Last time you mentioned a headache"). That recap got
    mis-extracted as a fresh caller fact, re-entered the new summary, was
    injected into the next call, recited again… a self-reinforcing loop that
    kept stale symptoms alive forever and drowned out what the caller newly
    said. Speaker labels + the prompt rule below break the loop.

    Falls back to the flat `transcript`/`transcription` string when there is
    no conversation_log (older call_logs, or the analyzer hasn't run yet).
    """
    convo = call_log.get("conversation_log")
    if isinstance(convo, list) and convo:
        lines: List[str] = []
        for turn in convo:
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role") or "").strip().lower()
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            speaker = "Caller" if role == "user" else "Assistant"
            lines.append(f"{speaker}: {text}")
        if lines:
            return "\n".join(lines)
    # Fallback: flat, unlabeled transcript (best effort).
    return (call_log.get("transcript") or call_log.get("transcription") or "").strip()


_EXTRACTION_SYSTEM_PROMPT = (
    "You extract a structured summary from the transcript of a SINGLE phone "
    "call between an AI voice assistant and a person it called.\n"
    "\n"
    "The transcript is speaker-labeled: lines starting 'Caller:' are the "
    "PERSON; lines starting 'Assistant:' are the AI. (If a line has no "
    "label, treat it as the Caller.)\n"
    "\n"
    "CRITICAL RULE — summarize ONLY what the CALLER newly said in THIS call:\n"
    "  • The Assistant usually OPENS by recapping earlier calls — e.g. "
    "\"Last time we spoke you mentioned a headache and back pain.\" That "
    "recap is NOT new information. NEVER extract anything the Assistant "
    "claims the caller said before. Ignore it completely.\n"
    "  • A fact belongs in the summary ONLY if the CALLER states or clearly "
    "confirms it in THIS transcript. If only the Assistant mentions "
    "something and the Caller never says it here, leave it out.\n"
    "  • If the Caller said nothing substantive this call (only 'yes', "
    "'okay', 'go ahead'), return ALL empty arrays and an empty raw_summary. "
    "Do NOT backfill from the Assistant's recap.\n"
    "\n"
    "Be CONSERVATIVE — leave a field empty rather than hallucinate. Use the "
    "caller's own words for `key_facts`. Output ONLY valid JSON matching:\n"
    "{\n"
    '  "key_facts":      [string],  // facts the CALLER newly stated this '
    "call. E.g. [\"leg pain\", \"swelling\", \"fever since morning\"].\n"
    '  "outcomes":       [string],  // what got decided/agreed this call. '
    "E.g. [\"booked May 22 11am\", \"declined appointment\"].\n"
    '  "follow_up_items":[string],  // open items the next call should '
    "revisit. E.g. [\"ask about dizziness\", \"confirm document upload\"].\n"
    '  "sentiment":      "positive"|"neutral"|"negative",\n'
    '  "raw_summary":    string     // 3-4 sentence prose, past tense, '
    "<=600 chars, describing only THIS call.\n"
    "}\n"
    "\n"
    "If the transcript has nothing the caller newly said, return all empty "
    "arrays and an empty raw_summary string. Do NOT explain — output JSON "
    "only."
)


async def _run_extraction_llm(
    transcript: str,
    *,
    client,
) -> Optional[CallSummarySchema]:
    """Call OpenAI in JSON mode and parse. Returns None on irrecoverable
    failure (caller logs and moves on). One retry on JSON-parse failure
    with a stricter user-message reminder — gpt-4o-mini occasionally
    pre-fixes the JSON with a sentence; the retry catches that.

    The retry ALSO doubles the completion-token budget: if the first attempt
    truncated mid-JSON (finish_reason="length"), parsing it would fail with a
    misleading "bad JSON" error. We detect finish_reason="length" explicitly,
    skip the doomed parse, and retry with headroom. (QA finding — long,
    content-rich calls were silently failing extraction with reason
    "llm_parse" that hid the real cause.)
    """
    user_msg_primary = f"Transcript:\n\n{transcript}\n\nReturn the JSON summary now."
    user_msg_retry = (
        "The previous response was not valid JSON (it may have been cut off "
        "or prefaced with text). Output ONLY the JSON object — no preface, no "
        "markdown, no code fences — and keep it concise. "
        f"Transcript:\n\n{transcript}"
    )

    # (user_message, max_completion_tokens). The retry gets a larger budget so
    # a length-truncation on attempt 1 has room to complete on attempt 2.
    attempts = [(user_msg_primary, 600), (user_msg_retry, 1200)]

    for attempt, (user_msg, max_toks) in enumerate(attempts, start=1):
        try:
            resp = await client.chat.completions.create(
                model=_EXTRACTION_MODEL,
                messages=[
                    {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_completion_tokens=max_toks,
                timeout=30.0,
            )
            choice = resp.choices[0]
            raw = choice.message.content or "{}"
            finish_reason = getattr(choice, "finish_reason", None)
        except Exception:
            logger.exception("[SUMMARY] LLM call failed (attempt %d)", attempt)
            continue

        # A "length" finish means the JSON was cut off mid-object — parsing it
        # is guaranteed to fail, and the failure would be misreported as a
        # parse error. Skip straight to the retry (which has 2x the budget).
        if finish_reason == "length":
            logger.warning(
                "[SUMMARY] attempt %d hit the token cap (finish_reason=length, "
                "max_completion_tokens=%d) — JSON truncated; %s",
                attempt, max_toks,
                "retrying with a larger budget" if attempt < len(attempts)
                else "giving up (transcript too rich for the cap)",
            )
            continue

        try:
            data = json.loads(raw)
            parsed = CallSummarySchema.model_validate(data)
            if attempt > 1:
                logger.info("[SUMMARY] LLM returned valid JSON on retry %d", attempt)
            return parsed
        except (json.JSONDecodeError, ValidationError) as e:
            logger.warning(
                "[SUMMARY] attempt %d JSON parse failed: %s (raw[:200]=%r)",
                attempt, e, raw[:200],
            )
            continue

    logger.warning("[SUMMARY] giving up after 2 attempts")
    return None


def _persist_summary_sync(
    *,
    call_log_id: ObjectId,
    contact_id: ObjectId,
    user_id: ObjectId,
    assistant_id: Optional[ObjectId],
    call_date: datetime,
    summary: CallSummarySchema,
) -> bool:
    """Sync Mongo write. Idempotent via the unique index on call_log_id;
    a duplicate-key race is treated as success."""
    try:
        db = Database.get_db()
        now = datetime.now(timezone.utc)
        doc = {
            "contact_id": contact_id,
            "call_log_id": call_log_id,
            "user_id": user_id,
            "assistant_id": assistant_id,
            "date": call_date,
            "key_facts": list(summary.key_facts),
            "outcomes": list(summary.outcomes),
            "follow_up_items": list(summary.follow_up_items),
            "sentiment": summary.sentiment,
            "raw_summary": summary.raw_summary,
            "extraction_model": _EXTRACTION_MODEL,
            "extraction_version": EXTRACTION_VERSION,
            "created_at": now,
        }
        # Upsert keyed by call_log_id. `$setOnInsert` ensures a second
        # concurrent extraction doesn't overwrite a perfectly-good earlier
        # summary with potentially-different LLM output (extractions are
        # stochastic even at temperature 0 on rare occasions).
        res = db["call_summaries"].update_one(
            {"call_log_id": call_log_id},
            {"$setOnInsert": doc},
            upsert=True,
        )
        if res.upserted_id:
            logger.info(
                "[SUMMARY] persisted summary=%s for call_log=%s contact=%s",
                res.upserted_id, call_log_id, contact_id,
            )
        else:
            logger.info(
                "[SUMMARY] summary already existed for call_log=%s — skipped (idempotent)",
                call_log_id,
            )
        return True
    except Exception:
        logger.exception("[SUMMARY] persist failed for call_log=%s", call_log_id)
        return False


async def extract_and_persist_summary(call_log_id: str) -> Dict[str, Any]:
    """Top-level entry point. Called from the Twilio post-transcription
    path AND any future backfill admin endpoint.

    Returns a status dict suitable for the caller to log; never raises
    (so a webhook handler can't be brought down by a bad transcript).

    Status values:
      • "ok"                          — summary written
      • "skipped_existing"             — already extracted (idempotent no-op)
      • "skipped_opt_out"              — contact has do_not_remember=True
      • "skipped_short_transcript"     — call too short to extract from
      • "skipped_history_disabled"     — assistant doesn't have
                                          conversation_history_enabled
      • "skipped_no_phone"             — call_log has no resolvable phone
      • "skipped_no_user"              — call_log has no resolvable user_id
      • "skipped_call_log_not_found"   — bad input
      • "failed_extraction"            — LLM gave up after retries
      • "failed_persist"               — Mongo write failed
    """
    if not ObjectId.is_valid(call_log_id):
        return {"status": "skipped_call_log_not_found", "reason": "invalid_call_log_id"}

    # 1. Fetch the call_log.
    def _fetch_call_log():
        try:
            return Database.get_db()["call_logs"].find_one({"_id": ObjectId(call_log_id)})
        except Exception:
            logger.exception("[SUMMARY] call_log fetch failed: %s", call_log_id)
            return None

    call_log = await asyncio.to_thread(_fetch_call_log)
    if not call_log:
        return {"status": "skipped_call_log_not_found"}

    # Build a SPEAKER-LABELED transcript ("Caller:" / "Assistant:" lines)
    # from conversation_log when available, else fall back to the flat
    # transcript/transcription string. The labels are what let the extractor
    # ignore the assistant's prior-call recap instead of re-summarizing it
    # into the new summary (the self-reinforcing-memory bug).
    transcript = _build_labeled_transcript(call_log)
    if len(transcript) < _MIN_TRANSCRIPT_CHARS:
        return {"status": "skipped_short_transcript", "chars": len(transcript)}
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        # Truncate. Keep both ends — the start has the greeting + topic, the
        # end has the closing decisions. We'd lose middle context, which is
        # acceptable: an extreme transcript is usually a single rambling
        # client and the LLM only needs structure.
        head = transcript[: _MAX_TRANSCRIPT_CHARS // 2]
        tail = transcript[-_MAX_TRANSCRIPT_CHARS // 2 :]
        transcript = head + "\n\n[...transcript truncated...]\n\n" + tail

    # 2. Gate on assistant.conversation_history_enabled.
    assistant_id = call_log.get("assistant_id")
    assistant_obj_id = (
        assistant_id if isinstance(assistant_id, ObjectId)
        else (ObjectId(assistant_id) if (isinstance(assistant_id, str) and ObjectId.is_valid(assistant_id)) else None)
    )

    def _fetch_assistant_flag():
        try:
            if assistant_obj_id is None:
                return None
            a = Database.get_db()["assistants"].find_one(
                {"_id": assistant_obj_id},
                {"conversation_history_enabled": 1, "user_id": 1},
            )
            return a
        except Exception:
            logger.exception("[SUMMARY] assistant fetch failed: %s", assistant_obj_id)
            return None

    assistant = await asyncio.to_thread(_fetch_assistant_flag)
    if not assistant or not bool(assistant.get("conversation_history_enabled")):
        return {"status": "skipped_history_disabled"}

    # 3. Resolve user_id (prefer call_log.user_id; fall back to assistant.user_id).
    raw_user_id = call_log.get("user_id") or assistant.get("user_id")
    if not raw_user_id:
        return {"status": "skipped_no_user"}

    # 4. Resolve / create contact for this phone number.
    # MUST use the shared resolver — the pre-call read path (agent_worker →
    # build_context_block) calls the SAME helper, so the summary is written
    # under the exact contact the next call will look up. Diverging here was
    # a real bug that made conversation memory silently no-op.
    customer_phone = resolve_contact_phone(
        direction=call_log.get("direction"),
        from_number=call_log.get("from_number"),
        to_number=call_log.get("to_number"),
        customer_phone=call_log.get("customer_phone"),
    )
    if not customer_phone:
        return {"status": "skipped_no_phone"}

    # Pass the lead's name as a hint if we have one.
    name_hint = call_log.get("customer_name") or call_log.get("lead_name")
    contact = await get_or_create_contact(
        user_id=raw_user_id,
        phone_number=customer_phone,
        name_hint=name_hint,
    )
    if not contact:
        return {"status": "skipped_no_phone", "reason": "phone_could_not_be_normalized"}

    # 5. Honour the opt-out.
    if contact.get("do_not_remember"):
        return {"status": "skipped_opt_out", "contact_id": str(contact["_id"])}

    # 6. Run extraction.
    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("[SUMMARY] OPENAI_API_KEY not set; cannot extract")
        return {"status": "failed_extraction", "reason": "no_openai_key"}

    try:
        from openai import AsyncOpenAI
    except Exception:
        logger.warning("[SUMMARY] openai SDK not importable")
        return {"status": "failed_extraction", "reason": "no_openai_sdk"}

    client = AsyncOpenAI()
    parsed = await _run_extraction_llm(transcript, client=client)
    if parsed is None:
        return {"status": "failed_extraction", "reason": "llm_parse"}

    # 6b. Sanitize before persistence. This drops any extracted string that
    # looks like a prompt-injection attempt (e.g. caller said "ignore your
    # previous instructions" and the LLM dutifully captured it). Combined
    # with the untrusted-data disclaimer that conversation_history_service
    # wraps the injected block in, this is defence-in-depth against
    # cross-call prompt injection.
    parsed = _sanitize_summary(parsed)

    # 6c. Re-check the opt-out flag immediately before persisting. The LLM
    # call above can take 5-30s; in that window a user could have flipped
    # do_not_remember=True via the contacts API — and that flag's cascade
    # delete_many would have found NOTHING to delete because this summary
    # wasn't written yet. Re-reading here shrinks the TOCTOU window to the
    # few ms between this check and the upsert. Fail CLOSED: if we can't
    # confirm the contact is not opted out, skip the write — the backfill
    # loop re-runs the whole extraction (with a fresh opt-out check) later.
    def _recheck_opt_out() -> bool:
        try:
            c = Database.get_db()["contacts"].find_one(
                {"_id": contact["_id"]}, {"do_not_remember": 1},
            )
            return bool(c is None or c.get("do_not_remember"))
        except Exception:
            logger.exception(
                "[SUMMARY] opt-out re-check failed for contact=%s — "
                "failing closed (skip persist; backfill will retry)",
                contact["_id"],
            )
            return True

    if await asyncio.to_thread(_recheck_opt_out):
        return {
            "status": "skipped_opt_out",
            "contact_id": str(contact["_id"]),
            "reason": "opted_out_or_unconfirmed_during_extraction",
        }

    # 7. Persist.
    call_date = call_log.get("created_at")
    if not isinstance(call_date, datetime):
        # Missing, or a non-datetime (e.g. an ISO string from a legacy
        # writer) — don't blow up on `.tzinfo`; fall back to now().
        call_date = datetime.now(timezone.utc)
    elif call_date.tzinfo is None:
        call_date = call_date.replace(tzinfo=timezone.utc)

    user_obj_id = (
        raw_user_id if isinstance(raw_user_id, ObjectId)
        else (ObjectId(raw_user_id) if (isinstance(raw_user_id, str) and ObjectId.is_valid(raw_user_id)) else None)
    )
    if user_obj_id is None:
        return {"status": "skipped_no_user", "reason": "user_id_invalid"}

    ok = await asyncio.to_thread(
        _persist_summary_sync,
        call_log_id=ObjectId(call_log_id),
        contact_id=contact["_id"],
        user_id=user_obj_id,
        assistant_id=assistant_obj_id,
        call_date=call_date,
        summary=parsed,
    )
    if not ok:
        return {"status": "failed_persist"}

    return {
        "status": "ok",
        "contact_id": str(contact["_id"]),
        "call_log_id": call_log_id,
        "sentiment": parsed.sentiment,
        "n_key_facts": len(parsed.key_facts),
        "n_follow_ups": len(parsed.follow_up_items),
    }


# ── Backfill loop (catches orphans dropped by webhook fire-and-forget) ───────
# The webhook-side extraction is `asyncio.create_task(...)` which means the
# task is lost if the App Runner container restarts mid-extraction (deploy,
# autoscale, OOM). The call_log keeps the full transcript, but the
# corresponding call_summary row never lands → the contact's next call has
# no memory of this one. To recover those orphans, run a lightweight
# backfill every N minutes that:
#   1. finds call_logs with non-empty transcription AND no matching
#      call_summary AND assistant.conversation_history_enabled=True;
#   2. re-runs extract_and_persist_summary for each.
#
# Idempotency: the unique index on call_summaries.call_log_id makes
# re-extraction safe. If a fast webhook path also lands at the same time,
# one of the two writers gets DuplicateKey and silently drops — no data
# corruption.
#
# Bounded: only looks back 7 days (older orphans are usually genuine
# data-quality issues, not transient races) and processes max 20 per loop
# pass so a backlog burst doesn't pin the LLM budget. Runs every 10 min.
_BACKFILL_INTERVAL_SECONDS = 10 * 60
_BACKFILL_LOOKBACK_DAYS = 7
_BACKFILL_BATCH_SIZE = 20


async def _backfill_orphan_summaries_once() -> Dict[str, Any]:
    """One pass of the backfill — find orphan call_logs + re-extract.

    Returns a status dict for logging. NEVER raises (caller is a forever-
    loop and we don't want to die on a transient DB hiccup).
    """
    result: Dict[str, Any] = {"scanned": 0, "extracted": 0, "skipped": 0, "errors": 0}
    try:
        from datetime import timedelta
        db = Database.get_db()
        cutoff = datetime.now(timezone.utc) - timedelta(days=_BACKFILL_LOOKBACK_DAYS)

        # Find candidate call_logs: have transcription, end-recent, and the
        # assistant must have conversation_history_enabled. We don't bother
        # filtering on assistant.conversation_history_enabled in the Mongo
        # query (that requires a $lookup or two-stage fetch) — instead,
        # extract_and_persist_summary itself gates on the flag and returns
        # "skipped_history_disabled" cheaply.
        def _find_orphans():
            try:
                # $or covers both possible field names. The live transcription
                # path writes `transcript` (singular); some legacy paths write
                # `transcription`. We need to find call_logs that have EITHER
                # non-empty, otherwise the backfill loop misses every real
                # call. (Found in QA: previous filter on `transcription` only
                # never matched anything because the live writer uses
                # `transcript`.)
                pipeline = [
                    {"$match": {
                        "$and": [
                            {"created_at": {"$gte": cutoff}},
                            {"$or": [
                                {"transcript": {"$exists": True, "$nin": [None, ""]}},
                                {"transcription": {"$exists": True, "$nin": [None, ""]}},
                            ]},
                        ],
                    }},
                    {"$lookup": {
                        "from": "call_summaries",
                        "localField": "_id",
                        "foreignField": "call_log_id",
                        "as": "summary",
                    }},
                    {"$match": {"summary": {"$size": 0}}},
                    {"$project": {"_id": 1}},
                    {"$limit": _BACKFILL_BATCH_SIZE},
                ]
                return list(db["call_logs"].aggregate(pipeline))
            except Exception:
                logger.exception("[SUMMARY_BACKFILL] orphan-find failed")
                return []

        orphans = await asyncio.to_thread(_find_orphans)
        result["scanned"] = len(orphans)
        if not orphans:
            return result

        logger.info(
            "[SUMMARY_BACKFILL] found %d orphan call_logs (lookback=%d days)",
            len(orphans), _BACKFILL_LOOKBACK_DAYS,
        )
        for doc in orphans:
            try:
                res = await extract_and_persist_summary(str(doc["_id"]))
                status = res.get("status")
                if status == "ok":
                    result["extracted"] += 1
                else:
                    result["skipped"] += 1
                    logger.info(
                        "[SUMMARY_BACKFILL] call_log=%s → %s",
                        doc["_id"], status,
                    )
            except Exception:
                logger.exception(
                    "[SUMMARY_BACKFILL] extract failed for call_log=%s", doc["_id"],
                )
                result["errors"] += 1
        return result
    except Exception:
        logger.exception("[SUMMARY_BACKFILL] pass failed")
        result["errors"] += 1
        return result


async def summary_backfill_loop() -> None:
    """Forever-loop: invoke _backfill_orphan_summaries_once every
    `_BACKFILL_INTERVAL_SECONDS` seconds. Mirrors the cache_warmer_loop
    pattern in llm_cache_warmer.py — same start-side wiring in main.py.

    Wait one full interval before the first run so we don't compete with
    the initial-startup churn (cache warmer, index creation, etc.).
    """
    while True:
        try:
            await asyncio.sleep(_BACKFILL_INTERVAL_SECONDS)
            res = await _backfill_orphan_summaries_once()
            if res["scanned"] > 0:
                logger.info(
                    "[SUMMARY_BACKFILL] pass: scanned=%d extracted=%d skipped=%d errors=%d",
                    res["scanned"], res["extracted"], res["skipped"], res["errors"],
                )
        except asyncio.CancelledError:
            logger.info("[SUMMARY_BACKFILL] loop cancelled — exiting cleanly")
            raise
        except Exception:
            logger.exception("[SUMMARY_BACKFILL] loop iteration crashed; will retry")
