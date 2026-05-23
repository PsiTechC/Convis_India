"""Keeps OpenAI's prompt cache warm for every active assistant.

Why this exists: OpenAI's prompt cache TTL is ~5 minutes. If an assistant goes
that long without a call, the next call's first turn pays the full prompt-
processing cost (≈1s for a 1700-token system_message on gpt-4o-mini, longer on
gpt-4-turbo). For a real-estate agent that gets sporadic traffic, that's a bad
first impression on every call.

This warmer fires a tiny 1-token completion every ~4 minutes for each active
assistant, with the EXACT prompt assembly the agent uses at call time. Cache
stays warm 24/7 → every real call hits the warm path.

Cost per assistant: ~360 calls/day × ~$0.0005 (1 output token, mostly cached
input) ≈ $0.20/day. Cheap insurance.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Set

from app.config.database import Database

logger = logging.getLogger(__name__)

# Slightly under OpenAI's documented ~5 min cache TTL. Leaves margin for clock
# drift and request-processing time so we never miss the window.
WARMER_INTERVAL_SECONDS = 240

# OpenAI prompt caching only activates after ~1024 prompt tokens. Skip
# assistants whose system_message is too short to benefit.
MIN_SYSTEM_MESSAGE_CHARS = 4000  # ~1000 tokens at 4 chars/token average


async def _warm_one(client, assistant: dict, sent_keys: Set[str]) -> bool:
    """Fire a tiny LLM call to warm OpenAI's prompt cache for this assistant.
    Returns True if a request was sent. Skips assistants whose prompt is too
    short to be cached, and dedupes by content hash so two assistants sharing
    the same system_message only get one heartbeat.
    """
    base = assistant.get("system_message") or ""
    if len(base) < MIN_SYSTEM_MESSAGE_CHARS:
        return False

    # Build the EXACT prompt the agent sends at call time. Goes through the
    # shared `build_system_message` helper so calendar / expressive /
    # multilingual / RAG suffixes are all included identically. Any drift
    # between this and `assistant_config.load_assistant_config` → cache miss
    # → ~3-4s LLM TTFT regression on every turn for that assistant.
    # Imported lazily to avoid circular imports at module-load time.
    from app.services.livekit.assistant_config import (
        build_system_message,
        _coerce_expressive_mode,
        _coerce_multilingual_mode,
        _coerce_call_transfer_enabled,
        _coerce_call_transfer_number,
        _coerce_call_transfer_conditions,
        _coerce_outbound_followup_enabled,
        _coerce_e164_or_blank,
        _coerce_object_id_str,
        _coerce_str_field,
        _coerce_positive_int,
    )
    # NOTE: calendar_enabled requires per-assistant calendar_account lookups
    # (see assistant_config.load_assistant_config). For warmer purposes we
    # approximate from the boolean flag on the assistant doc — the worst case
    # if this is wrong is one turn of cache miss when the LIVE flag differs
    # from the flag on the doc; cache reseeds on the second turn either way.
    #
    # Call transfer: mirror load_assistant_config's "effective" flag (toggle
    # AND valid E.164 number) so the prompt suffix matches byte-for-byte.
    call_transfer_effective = bool(
        _coerce_call_transfer_enabled(assistant.get("call_transfer_enabled"))
        and _coerce_call_transfer_number(assistant.get("call_transfer_number"))
    )
    # Outbound follow-up: mirror load_assistant_config's "effective" gate.
    # Same fields, same coercers, same boolean logic → byte-identical prompt.
    followup_topic = _coerce_str_field(assistant.get("outbound_followup_topic"), max_len=60)
    followup_ca_name = _coerce_str_field(assistant.get("ca_name"), max_len=80)
    followup_ca_phone = _coerce_e164_or_blank(assistant.get("ca_phone"))
    followup_ca_cal = _coerce_object_id_str(assistant.get("ca_calendar_account_id"))
    followup_wa_client = _coerce_str_field(assistant.get("wa_template_client"), max_len=80)
    followup_wa_ca = _coerce_str_field(assistant.get("wa_template_ca"), max_len=80)
    followup_duration = _coerce_positive_int(
        assistant.get("appointment_duration_minutes"), default=30, lo=5, hi=240,
    )
    followup_tz = _coerce_str_field(assistant.get("appointment_timezone"), max_len=64)
    followup_effective = bool(
        _coerce_outbound_followup_enabled(assistant.get("outbound_followup_enabled"))
        and followup_ca_cal
        and followup_ca_phone
        and (followup_wa_client or followup_wa_ca)
    )
    system_message = build_system_message(
        base_message=base,
        calendar_enabled=bool(assistant.get("calendar_enabled")),
        timezone_hint=assistant.get("timezone") or "America/New_York",
        expressive_mode=_coerce_expressive_mode(assistant.get("expressive_mode")),
        multilingual=_coerce_multilingual_mode(assistant.get("multilingual")),
        has_knowledge_base=bool(assistant.get("knowledge_base_files")),
        call_transfer_enabled=call_transfer_effective,
        call_transfer_conditions=_coerce_call_transfer_conditions(assistant.get("call_transfer_conditions")),
        outbound_followup_enabled=followup_effective,
        outbound_followup_topic=followup_topic,
        outbound_followup_ca_name=followup_ca_name,
        outbound_followup_duration_minutes=followup_duration,
        outbound_followup_timezone=followup_tz,
    )

    # Dedupe by hash of the actual prompt prefix that hits OpenAI's cache.
    key = hashlib.sha256(system_message.encode("utf-8")).hexdigest()
    if key in sent_keys:
        return False
    sent_keys.add(key)

    # Explicit prompt_cache_key per assistant — must match what the agent_worker
    # passes when constructing openai.LLM(prompt_cache_key=assistant_id). Without
    # this, OpenAI auto-infers a cache key from the prompt prefix; that
    # inference is unreliable across the bare-SDK warmer vs livekit's wrapped
    # chat() call → cache misses. Setting a deterministic key makes both routes
    # share the same cache bucket reliably.
    cache_key = str(assistant.get("_id")) if assistant.get("_id") is not None else "unknown-assistant"
    try:
        # `prompt_cache_key` was sent via extra_body before — but the openai
        # SDK >=2.x exposes it as a NATIVE top-level parameter. The agent uses
        # the native param (via livekit-plugins-openai). Sending via extra_body
        # vs native may take different code paths in the SDK and produce
        # subtly different request fingerprints → cache miss. Pass it natively
        # to guarantee the warmer and agent hit the same cache shard.
        await client.chat.completions.create(
            model=assistant.get("llm_model") or "gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": "."},
            ],
            max_completion_tokens=1,
            temperature=0.0,
            timeout=15.0,
            prompt_cache_key=cache_key,
        )
        return True
    except Exception:
        logger.warning(
            "LLM cache warm failed for assistant %s",
            assistant.get("_id"),
            exc_info=True,
        )
        return False


async def _warm_all_once() -> None:
    """One pass: walk all assistants in Mongo and warm each unique prompt."""
    if not os.getenv("OPENAI_API_KEY"):
        logger.debug("OPENAI_API_KEY not set; skipping cache warm")
        return

    try:
        from openai import AsyncOpenAI
    except Exception:
        logger.warning("openai package not importable; skipping cache warm")
        return

    client = AsyncOpenAI()
    db = Database.get_db()
    # Pull all fields the warmer needs to reconstruct the EXACT prompt the
    # agent sends. Adding a field to build_system_message() requires adding it
    # to this projection.
    assistants = list(db["assistants"].find(
        {},
        {
            "_id": 1,
            "system_message": 1,
            "expressive_mode": 1,
            "multilingual": 1,
            "calendar_enabled": 1,
            "timezone": 1,
            "knowledge_base_files": 1,
            "llm_model": 1,
            "call_transfer_enabled": 1,
            "call_transfer_number": 1,
            "call_transfer_conditions": 1,
            # Outbound-follow-up fields — must be projected here so the warmer
            # builds the same prompt prefix as the live agent. Missing any
            # → cache miss → ~3-4 s LLM TTFT regression on every turn.
            "outbound_followup_enabled": 1,
            "outbound_followup_topic": 1,
            "ca_name": 1,
            "ca_phone": 1,
            "ca_calendar_account_id": 1,
            "wa_template_client": 1,
            "wa_template_ca": 1,
            "appointment_duration_minutes": 1,
            "appointment_timezone": 1,
        },
    ))

    sent_keys: Set[str] = set()
    warmed = 0
    for a in assistants:
        if await _warm_one(client, a, sent_keys):
            warmed += 1
        # Gentle pacing — avoids bursting OpenAI's rate limiter on shops with
        # many assistants. 50 ms × 100 assistants = 5 s, well under interval.
        await asyncio.sleep(0.05)

    logger.info(
        "[LLM_CACHE_WARMER] warmed %d unique prompts across %d assistants",
        warmed,
        len(assistants),
    )


async def cache_warmer_loop() -> None:
    """Background loop, runs for the lifetime of the FastAPI process. Each
    iteration warms every active assistant's prompt; sleep, repeat.
    """
    logger.info(
        "[LLM_CACHE_WARMER] starting; interval=%ds",
        WARMER_INTERVAL_SECONDS,
    )
    # Initial small delay so we don't compete with other startup work.
    await asyncio.sleep(15)
    while True:
        try:
            await _warm_all_once()
        except Exception:
            logger.exception("[LLM_CACHE_WARMER] iteration failed")
        await asyncio.sleep(WARMER_INTERVAL_SECONDS)
