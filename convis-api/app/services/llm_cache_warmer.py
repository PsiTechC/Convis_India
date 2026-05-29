"""Keeps the LLM provider warm for every active assistant.

Why this exists: a sporadic-traffic assistant pays the full first-turn cost on
every cold call. This loop fires a tiny 1-token completion every ~4 minutes per
unique assistant prompt, with the EXACT prompt assembly the agent uses at call
time, so the provider edge (connection routing, model warm pool) stays hot.

Provider handling:
- OpenAI (legacy): the call also primes OpenAI's ~5-min prompt cache via the
  explicit prompt_cache_key, so the next real call's prefix is cached server-side.
- Sarvam (current live stack): Sarvam's OpenAI-compatible API has NO prompt
  cache (verified — usage returns no cached_tokens, and `prompt_cache_key` is
  not an accepted param). So on Sarvam this loop does NOT cache tokens; it keeps
  the HTTPS/edge path + model routing warm for the assistant. The per-turn
  prompt-processing cost is unchanged; only cold-start jitter is reduced.

The warmer auto-selects the provider: Sarvam when SARVAM_API_KEY is set,
otherwise OpenAI when OPENAI_API_KEY is set.

Cost: 1 output token per assistant per ~4 min. Negligible.
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

# Only warm assistants whose prompt is non-trivial. On OpenAI prompt caching
# activates after ~1024 prompt tokens; on Sarvam there's no cache but a short
# prompt warms instantly anyway, so this just avoids pointless heartbeats.
MIN_SYSTEM_MESSAGE_CHARS = 4000  # ~1000 tokens at 4 chars/token average


async def _warm_one(client, assistant: dict, sent_keys: Set[str], provider: str) -> bool:
    """Fire a tiny LLM call to warm the provider for this assistant.

    On OpenAI this also primes the server-side prompt cache (via prompt_cache_key).
    On Sarvam there is no prompt cache, so it warms the connection/model routing
    only. Returns True if a request was sent. Skips assistants whose prompt is too
    short to be worth warming, and dedupes by content hash so two assistants
    sharing the same system_message only get one heartbeat.
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

    # Dedupe by hash of the prompt prefix. Two assistants with an identical
    # system_message warm once (and on OpenAI share the same cache bucket).
    key = hashlib.sha256(system_message.encode("utf-8")).hexdigest()
    if key in sent_keys:
        return False
    sent_keys.add(key)

    cache_key = str(assistant.get("_id")) if assistant.get("_id") is not None else "unknown-assistant"
    try:
        if provider == "sarvam":
            # Sarvam (OpenAI-compatible /v1, no prompt cache). Default model is
            # the live sarvam-105b. `/nothink` already lives in system_message
            # via build_system_message, so we don't re-add it. max_tokens (not
            # max_completion_tokens — Sarvam uses the legacy name) capped at 1.
            # No prompt_cache_key: Sarvam rejects/ignores it. This warms the
            # connection + model routing, not a token cache.
            await client.chat.completions.create(
                model=assistant.get("llm_model") or "sarvam-105b",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": "."},
                ],
                max_tokens=1,
                temperature=0.0,
                timeout=15.0,
            )
        else:
            # OpenAI: explicit prompt_cache_key per assistant — must match what
            # agent_worker passes so the warmer and the live call share the same
            # server-side cache shard. Native top-level param (SDK >=2.x).
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
            "LLM warm failed for assistant %s (provider=%s)",
            assistant.get("_id"), provider,
            exc_info=True,
        )
        return False


async def _warm_all_once() -> None:
    """One pass: walk all assistants in Mongo and warm each unique prompt.

    Provider auto-select: Sarvam if SARVAM_API_KEY is set (the live India stack),
    else OpenAI if OPENAI_API_KEY is set, else skip.
    """
    sarvam_key = os.getenv("SARVAM_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    try:
        from openai import AsyncOpenAI
    except Exception:
        logger.warning("openai package not importable; skipping LLM warm")
        return

    if sarvam_key:
        provider = "sarvam"
        # Sarvam exposes an OpenAI-compatible API; auth is via the
        # `api-subscription-key` header, not Bearer. The AsyncOpenAI client
        # sends Authorization: Bearer by default, so we add the Sarvam header
        # explicitly and point base_url at Sarvam's /v1.
        client = AsyncOpenAI(
            api_key=sarvam_key,
            base_url="https://api.sarvam.ai/v1",
            default_headers={"api-subscription-key": sarvam_key},
        )
    elif openai_key:
        provider = "openai"
        client = AsyncOpenAI()
    else:
        logger.debug("Neither SARVAM_API_KEY nor OPENAI_API_KEY set; skipping LLM warm")
        return

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
        if await _warm_one(client, a, sent_keys, provider):
            warmed += 1
        # Gentle pacing — avoids bursting the provider's rate limiter on shops
        # with many assistants. 50 ms × 100 assistants = 5 s, well under interval.
        await asyncio.sleep(0.05)

    logger.info(
        "[LLM_CACHE_WARMER] provider=%s warmed %d unique prompts across %d assistants",
        provider,
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
