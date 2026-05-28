"""Load an assistant from MongoDB into the runtime config used by the LiveKit agent.

Shared by the FastAPI process (for browser token dispatch) and the agent worker
process (which reads room metadata to find the assistant_id, then calls this).

Convis shares its MongoDB with other projects that store assistants with
different ASR/LLM/TTS provider values. This loader coerces any non-supported
value back to the locked Convis-India stack — Sarvam Saaras v3 ASR +
Sarvam-105b LLM + Sarvam Bulbul TTS — so a foreign project's writes can never
crash a Convis call (e.g. nova-2-phonecall, gpt-4o-mini, or ElevenLabs voice
IDs from legacy docs).

Migrations:
  - 2026-05-23 TTS: ElevenLabs + Cartesia removed in favour of Sarvam Bulbul.
  - 2026-05-23 LLM: OpenAI removed in favour of Sarvam-105b. build_system_message()
    prepends "/nothink" so sarvam-105b skips its <think>...</think> reasoning
    blocks (which would add 2-5s of latency per turn).
  - 2026-05-28 ASR: Deepgram removed in favour of Sarvam Saaras v3 with
    mode=transcribe. Multilingual mode now maps to language="unknown" (Sarvam's
    auto-detect across 22 Indic languages + en-IN).
Old Mongo assistants still carry stale provider values; the coercion layer
maps them to Sarvam defaults at load time without rewriting the docs.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from bson import ObjectId

from app.config.database import Database
from app.config.settings import settings

logger = logging.getLogger(__name__)


# ── Locked provider whitelists ───────────────────────────────────────────────
# Sarvam Saaras / Saarika — Indic-language ASR (post-migration 2026-05-28).
# Saaras v3 supports all 22 Indic languages plus en-IN in transcribe mode;
# Saarika v2.5 covers 11 of those. Saaras v2.5 is translate-only (forces
# English output) — kept in the whitelist for explicit opt-in but not the
# default, because voice agents need source-language transcripts so the LLM
# and TTS can reply in the caller's language.
_SARVAM_ASR_MODELS = {"saaras:v3", "saarika:v2.5", "saaras:v2.5"}
_SARVAM_DEFAULT_ASR_MODEL = "saaras:v3"

# Saaras v3 modes — `transcribe` keeps source language, `translate` forces
# English output. The others (verbatim, translit, codemix) are Sarvam-specific
# transformations useful in narrower cases.
_SARVAM_ASR_MODES = {"transcribe", "translate", "verbatim", "translit", "codemix"}
_SARVAM_DEFAULT_ASR_MODE = "transcribe"

# Sarvam ASR languages (BCP-47 India-locale). "unknown" → server-side
# auto-detection across all supported languages, used when multilingual mode
# is on. Saarika v2.5 supports the first 12 codes; Saaras v3 supports all 23.
_SARVAM_ASR_LANGUAGES = {
    "unknown",
    # saarika:v2.5 + saaras:v3
    "en-IN", "hi-IN", "bn-IN", "gu-IN", "kn-IN", "ml-IN",
    "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN",
    # saaras:v3-only (Northeast / less-resourced langs)
    "as-IN", "ur-IN", "ne-IN", "kok-IN", "ks-IN", "sd-IN",
    "sa-IN", "sat-IN", "mni-IN", "brx-IN", "mai-IN", "doi-IN",
}
_SARVAM_DEFAULT_ASR_LANGUAGE = "en-IN"
# Sarvam LLM whitelist. sarvam-105b is the flagship; sarvam-m is the smaller/
# faster alternative. -16k / -32k suffixes are deprecated by Sarvam but kept
# in the whitelist for backward compatibility (the plugin still accepts them).
_SARVAM_LLM_MODELS = {
    "sarvam-m",
    "sarvam-30b",
    "sarvam-30b-16k",   # deprecated by Sarvam
    "sarvam-105b",
    "sarvam-105b-32k",  # deprecated by Sarvam
}
_SARVAM_DEFAULT_LLM_MODEL = "sarvam-105b"
# Sarvam Bulbul — the only supported TTS provider for Convis-India.
#
# Model defaults to bulbul:v3 — the streaming-capable flagship. The
# livekit-plugins-sarvam 1.5.12 plugin handles v3 correctly (sends
# temperature + omits pitch/loudness for v3 per Sarvam's v3 API). v2 is kept
# in the whitelist as opt-in for assistants that need v2-only speakers
# (anushka, manisha, vidya, arya, abhilash, karun, hitesh).
_SARVAM_TTS_MODELS = {"bulbul:v2", "bulbul:v3", "bulbul:v3-beta"}
_SARVAM_DEFAULT_MODEL = "bulbul:v3"

# Sarvam Bulbul speaker names (per livekit-plugins-sarvam 1.5.12's
# MODEL_SPEAKER_COMPATIBILITY table). The plugin enforces per-model
# compatibility — sending a v2-only speaker like "anushka" to bulbul:v3
# raises ValueError on TTS instantiation. We track v2 and v3 speakers
# separately so _coerce_tts_voice can pick a model-appropriate default.
#
# v2 speakers (7): the original Bulbul set — anushka is the recognisable
# female Indian-English voice. Confirmed NOT supported on v3.
_SARVAM_V2_SPEAKERS = {
    "anushka", "manisha", "vidya", "arya",          # female
    "abhilash", "karun", "hitesh",                  # male
}

# v3 speakers (30): broader customer-care, conversational, narrator set.
# shubh (male Hindi) is the plugin's auto-default when speaker=None + v3,
# and our locked Convis-India default.
_SARVAM_V3_SPEAKERS = {
    # female
    "ritu", "pooja", "simran", "kavya", "ishita", "shreya", "priya",
    "neha", "roopa", "amelia", "sophia",
    "suhani", "rupali", "tanya", "shruti", "kavitha",
    # male
    "shubh", "rahul", "amit", "ratan", "rohan", "dev", "manan", "sumit",
    "aditya", "kabir", "varun", "aayan", "ashutosh", "advait",
}

# Union — used as the "is this a recognised Sarvam speaker at all?" check.
_SARVAM_TTS_SPEAKERS = _SARVAM_V2_SPEAKERS | _SARVAM_V3_SPEAKERS

_SARVAM_DEFAULT_SPEAKER = "shubh"  # male Hindi, v3-compatible

# Sarvam Bulbul languages (BCP-47, India-locale). en-IN is the Convis default.
# Saaras:v3 supports more Indic languages on the ASR side, but Bulbul's TTS
# catalogue is limited to these 11 — keep ASR/TTS language coercion separate.
_SARVAM_TTS_LANGUAGES = {
    "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN",
    "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN",
}
_SARVAM_DEFAULT_LANGUAGE = "en-IN"

# Single supported provider after the 2026-05-23 migration. Kept as a set so
# the coercer's API is unchanged and future additions are easy.
_TTS_PROVIDERS = {"sarvam"}


# ── Expressive mode ──────────────────────────────────────────────────────────
# Opt-in toggle that swaps the default ElevenLabs Flash model (fast, robotic)
# for ElevenLabs v3 (slower, but renders inline emotion tags like [laughs],
# [sighs], [whispers], [coughs]). Combined with the prompt suffix below, the
# LLM emits emotion tags occasionally and the v3 TTS engine renders them as
# real laughs / sighs / coughs instead of speaking the bracketed letters.
#
# Tradeoff: v3 TTFB is ~400-700ms vs flash's ~150ms. Only the assistant owner
# should opt in; the locked production default stays flash for latency reasons.
#
# Cartesia voices are inherently more emotive — for Cartesia we keep the same
# model but still inject the prompt suffix so the LLM uses natural fillers
# and pacing cues (commas, ellipses) that Cartesia renders well.
_EXPRESSIVE_PROMPT_SUFFIX = """
---
Speaking style: You are speaking aloud over a phone call — sound natural and human, not robotic. Tools (use sparingly):

1. Light conversational fillers — "well", "okay", "right", "actually", "hmm". One per turn at most, only when a human would naturally pause or think. Never on every sentence.
2. Pacing via punctuation — commas, ellipses (…), short sentences. Long monologues sound robotic; conversational rhythm sounds human.
3. Contractions — prefer "I'm" over "I am", "don't" over "do not", "you're" over "you are".

Stay professional and measured. Do NOT use spelled-out laughs ("haha"), exclamations ("oh nice", "wow", "oof"), or hype phrases — these sound jarring in formal contexts (callers asking about products, prices, policies). Match the caller's tone: keep it warm but composed."""


# Multilingual mode: opt-in. When enabled, ASR runs in language="unknown"
# (Sarvam Saaras v3 auto-detects per utterance across 22 Indic languages +
# en-IN, with native code-switching support) and the LLM is instructed to
# match the caller's language. Sarvam Bulbul TTS supports the 11 most common
# Indic languages from the same speaker, so no TTS change is needed for
# multilingual flows that stick to those languages.
#
# Trade-off: pinning a specific language code (e.g. "hi-IN") gives Sarvam
# tighter accuracy than auto-detect on that language. Enable multilingual
# only for assistants that genuinely expect callers in multiple languages.
_MULTILINGUAL_PROMPT_SUFFIX = """
---
LANGUAGE MATCHING — CRITICAL OVERRIDE:

This system prompt is written in English, but YOU MUST NOT default to English. On EVERY user turn, look at the user's actual words and detect their language from the script and content:
- Devanagari script (मुझे, क्या, हिंदी) → Hindi or Marathi → reply in same script
- Arabic script (مرحبا, شكرا) → Arabic → reply in Arabic
- CJK characters (你好, 谢谢) → Chinese → reply in Chinese
- Hindi/Urdu words written in Roman ("kya hai", "dhanyavaad", "shukriya", "namaste", "alvida", "haan", "kaise") → Hindi → reply in Devanagari Hindi
- Latin script with English words → English

REPLY in the EXACT SAME language the caller used in their MOST RECENT message — not the language of the system prompt, not the language of earlier turns. If the caller switches mid-conversation, switch with them on the very next reply. If the caller mixes (e.g. "Mujhe Dubai ke baare mein bataiye"), reply in the dominant language of their message.

You MUST NOT ask the caller to switch languages. You MUST NOT say "should we continue in Hindi?" or "I can speak Hindi if you prefer". Just MATCH them silently.

Proper nouns (brand names, place names, your assistant's own name) may stay in their original language inside an otherwise-localized reply.

If you are uncertain about the language, default to the script the caller used most recently. NEVER default to English just because the system prompt is in English.

NUMBERS — ALWAYS WESTERN DIGITS:
Phone numbers, prices, addresses, OTPs, account IDs, dates, times, and any other numeric value MUST be written in Western Arabic numerals (0 1 2 3 4 5 6 7 8 9) regardless of the conversation's language. Do NOT use Devanagari numerals (०१२३४५६७८९), Eastern Arabic-Indic numerals (٠١٢٣٤٥٦٧٨٩), or any other script's digit forms — those break phone dialers, regex validators, and storage systems. Example: a Hindi-language reply about a phone number should read "आपका नंबर है 8850501889" (NOT "८८५०५०१८८९" and NOT "٨٨٥٠٥٠١٨८٩")."""


# Knowledge-base RAG tool-use suffix.
#
# CRITICAL DESIGN: do NOT instruct the model to "speak a filler first then call
# the tool." OpenAI's chat-completions API treats text content and tool calls
# as MUTUALLY EXCLUSIVE in a single response: the LLM either returns text OR
# returns a tool call. When biased toward "speak filler first" inside a long
# system prompt (~5K chars), gpt-4o-mini follows the first half (text) and
# never emits the tool call → the agent says "let me check" and the tool never
# runs → caller waits forever. Verified locally: with the old suffix, the LLM
# called the tool 0/5 times against the real ACPCE prompt; with this suffix,
# it calls the tool 5/5 times. The 200-500 ms gap while the tool runs is
# acceptable; far better than a permanent dead-end.
#
# Also lives here (not in agent_worker.py) so the cache warmer can append the
# EXACT same text — any divergence breaks OpenAI prompt cache hits.
_RAG_TOOL_PROMPT_SUFFIX = (
    "\n\n---\n"
    "KNOWLEDGE BASE TOOL: When the caller asks anything specific about "
    "products, prices, dates, names, policies, procedures, lists, or any "
    "factual detail you'd need to look up — call the search_knowledge_base "
    "function with the caller's question. Do NOT answer factual questions "
    "from memory. Do NOT preface with 'let me check' or 'one moment' — just "
    "call the tool directly. The system handles the brief gap. After the tool "
    "returns results, answer naturally in 1-2 sentences based on what was "
    "retrieved. If the tool returns empty, say honestly: \"I don't have that "
    "in our official documentation — let me have a teammate follow up.\""
)


# End-call instruction. Always appended to EVERY assistant's system message
# (regardless of expressive / multilingual / KB / calendar settings) so any
# assistant can gracefully hang up. The end_call function tool is registered
# unconditionally on ConvisAgent. Lives here (not in agent_worker.py) so the
# cache warmer appends the same text — keeping prompt prefix byte-identical
# for OpenAI prompt-cache hits.
#
# CRITICAL DESIGN: the LLM passes the farewell text AS THE TOOL ARGUMENT
# (`farewell` param), not as separate text content. OpenAI treats text content
# and tool calls as mutually exclusive in a single response — when we asked
# for "speak farewell text AND call end_call", gpt-4o-mini returned text only
# 5/5 times (tool never fired). With the farewell as an argument, it fires
# 5/5 times correctly. The agent's end_call tool then speaks the farewell
# deterministically via session.say() before disconnecting.
_END_CALL_PROMPT_SUFFIX = (
    "\n\n---\n"
    "ENDING THE CALL: Call the end_call function ONLY when the caller "
    "clearly indicates the conversation is over via one of these patterns:\n"
    "(a) Explicit farewell word — 'bye', 'goodbye', 'good night', 'have a "
    "good day', 'talk to you later', 'catch you later', or the equivalent "
    "in their language ('alvida', 'au revoir', 'adios', '再见', 'ma'a "
    "salama').\n"
    "(b) Explicit conversation-end phrase — 'that's all', 'that is all', "
    "'that's it', 'we're done', 'I'm all set', 'no more questions', "
    "'nothing else'.\n\n"
    "Pass a brief one-sentence farewell as the `farewell` argument; the bot "
    "will speak it and disconnect.\n\n"
    "DO NOT call end_call on these (they are ACKNOWLEDGMENTS, not farewells):\n"
    "- 'ok', 'thanks', 'thank you', 'ok thanks', 'ok thank you' — caller is "
    "likely about to ask something else.\n"
    "- 'got it', 'understood', 'I see', 'right', 'cool', 'great', 'perfect' "
    "— acknowledgments.\n"
    "- 'thanks for that', 'thanks for the info', 'appreciate it' — gratitude "
    "for the previous answer; conversation continues.\n\n"
    "If unsure, do NOT call end_call. The 10-second idle timer will catch "
    "the call if the caller has actually left. A delayed disconnect is much "
    "better than dropping the caller mid-conversation."
)


# ── Call transfer to a human agent ───────────────────────────────────────────
# Opt-in per assistant. When `call_transfer_enabled` AND a valid E.164
# `call_transfer_number` are set, this suffix is appended to the system message
# and the `transfer_to_agent` function tool becomes meaningful. The LLM passes
# a brief `reason` as the TOOL ARGUMENT (not as separate spoken content — same
# rationale as _END_CALL_PROMPT_SUFFIX: gpt-4o-mini returns text-only when asked
# to "speak X AND call the tool"). The agent's transfer_to_agent tool then
# speaks the configured hold message deterministically and redirects the call.
#
# Lives here (not in agent_worker.py) so the cache warmer appends the identical
# text — keeping the OpenAI prompt prefix byte-identical for cache hits.
DEFAULT_TRANSFER_MESSAGE = "Let me connect you with a member of our team — please hold."

_CALL_TRANSFER_HEAD = (
    "\n\n---\n"
    "TRANSFERRING TO A HUMAN\n"
    "You have a tool `transfer_to_agent`. Call it (passing a one-phrase "
    "`reason`) when:\n"
    "  • the caller explicitly asks to speak to a person / human / agent / "
    "representative (incl. equivalents in other languages — \"hablar con una "
    "persona\", \"बात करनी है किसी से\", \"人と話したい\", etc.);\n"
    "  • the caller is clearly frustrated, repeating themselves, or you've "
    "already tried twice and still can't resolve their issue;\n"
    "  • the request is outside what you're set up to handle, per your role above."
)
_CALL_TRANSFER_TAIL = (
    "\n"
    "After calling transfer_to_agent, do NOT say anything else — the system "
    "tells the caller it's connecting them and completes the handoff. Do NOT "
    "transfer speculatively or for anything you can clearly handle yourself. "
    "Make at most ONE transfer attempt per call. If the tool returns "
    "\"transfer not available\", apologise once and continue helping as best "
    "you can."
)


def _build_call_transfer_suffix(conditions: str) -> str:
    """Assemble the call-transfer prompt suffix. `conditions` is the optional
    per-assistant 'extra when-to-transfer' text; when non-empty it's inserted
    as an additional bullet. Never leaves a literal placeholder or blank bullet."""
    extra = ""
    c = (conditions or "").strip()
    if c:
        extra = f"\n  • ADDITIONALLY, transfer when: {c}"
    return _CALL_TRANSFER_HEAD + extra + _CALL_TRANSFER_TAIL


# ── Outbound follow-up workflow (e.g. tax-attorney bot) ──────────────────────
# Opt-in per assistant. When effective (see is_followup_effective in
# outbound_followup_service.py), this suffix is appended to the system message
# and the `record_filing_status` + `book_followup_appointment` function tools
# become meaningful. The agent's tool wrappers re-check the same gates server-
# side; this suffix is purely about teaching the LLM the flow.
#
# Generic intentionally — the same flow works for renewal reminders, payment
# confirmations, survey calls, etc. The `topic` placeholder gets substituted
# from `outbound_followup_topic` so each vertical reads naturally aloud.
_FOLLOWUP_HEAD = (
    "\n\n---\n"
    "OUTBOUND FOLLOW-UP WORKFLOW ({topic})\n"
    "You are calling the client about {topic}. Run this flow:\n"
    "  1. Greet briefly and identify the firm.\n"
    "  2. Ask whether they have completed {topic}.\n"
    "  3. Call `record_filing_status` with `filed=true` or `filed=false` and "
    "a short `notes` field summarising what they said. Do this BEFORE moving "
    "on so the outcome is captured even if the line drops.\n"
    "  4. If they confirmed (filed=true): congratulate briefly, ask if there's "
    "anything else, then wrap up via `end_call`.\n"
    "  5. If they have NOT done it (filed=false): offer to book an appointment "
    "with {ca_name}. Propose a time (default duration: {duration} minutes, "
    "default timezone: {tz}). When the client confirms a slot:\n"
    "     • call `book_followup_appointment` with `start_iso` (full ISO-8601 "
    "with offset, e.g. 2026-05-22T11:00:00+05:30), `duration_minutes`, and the "
    "client's name as they gave it.\n"
    "     • the tool returns a short status. If it returns ok=true, tell the "
    "client the appointment is confirmed and that they'll receive a WhatsApp "
    "message shortly. If it returns ok=false, apologise once and offer to have "
    "someone call them back.\n"
    "  6. If the client declines the appointment outright, thank them and "
    "wrap up via `end_call` — do NOT call `book_followup_appointment`.\n"
    "\n"
    "Hard rules:\n"
    "  • Never invent appointment times. Only pass times the client explicitly "
    "agreed to.\n"
    "  • Never call `book_followup_appointment` before `record_filing_status` "
    "has been called with filed=false in this call.\n"
    "  • One booking per call maximum."
)


def _build_followup_suffix(*, topic: str, ca_name: str, duration: int, tz: str) -> str:
    """Substitute the runtime parameters into the follow-up suffix. Lives in
    its own helper (mirrors `_build_call_transfer_suffix`) so the cache warmer
    can produce the byte-identical string. Any non-string field is rendered
    via str() so a Mongo doc with int/float here never crashes the format."""
    return _FOLLOWUP_HEAD.format(
        topic=str(topic or "the follow-up topic"),
        ca_name=str(ca_name or "our specialist"),
        duration=int(duration or 30),
        tz=str(tz or "UTC"),
    )


def _calendar_suffix(timezone_hint: str) -> str:
    """Calendar scheduling suffix. Pulled into a helper so the cache warmer
    can build the exact same string as the agent."""
    return (
        "\n\n---\n"
        "Calendar Scheduling Instructions:\n"
        "You can schedule meetings during this call. When requested:\n"
        "1. Ask for preferred date and time\n"
        "2. Confirm the meeting title/purpose\n"
        "3. Confirm duration (default 30 minutes)\n"
        "4. Confirm their timezone\n"
        "5. Let them know you'll schedule it\n\n"
        f"Default timezone: {timezone_hint}"
    )


def build_system_message(
    *,
    base_message: str,
    calendar_enabled: bool,
    timezone_hint: str,
    expressive_mode: bool,
    multilingual: bool,
    has_knowledge_base: bool,
    call_transfer_enabled: bool = False,
    call_transfer_conditions: str = "",
    outbound_followup_enabled: bool = False,
    outbound_followup_topic: str = "",
    outbound_followup_ca_name: str = "",
    outbound_followup_duration_minutes: int = 30,
    outbound_followup_timezone: str = "",
) -> str:
    """Assemble the FULL system message the agent sends to OpenAI per turn.

    SINGLE SOURCE OF TRUTH. Both `load_assistant_config` (call-time) and
    `llm_cache_warmer` (background warming) MUST go through this function so
    the prompt prefix sent to OpenAI is byte-identical between the warmer and
    the live agent. Any divergence → cache miss → cold-start LLM TTFT on every
    turn (~3-4s vs ~0.5s warm).

    Order matters: suffix order must match between warmer and agent. The order
    used here matches the historical agent ordering (calendar, expressive,
    multilingual, RAG) — the call-transfer suffix slots in just before end_call
    (so the LAST instruction is still the most fundamental call-control one).

    `call_transfer_*` are defaulted so existing callers (and the warmer when an
    assistant has transfer off) produce byte-identical output to before this
    feature shipped.

    /nothink PREFIX (Sarvam-105b — added 2026-05-23): sarvam-105b emits
    <think>...</think> reasoning blocks by default, adding 2-5s of latency
    per turn. Prepending the literal token "/nothink" to the prompt disables
    this behaviour. The token is documented by Sarvam and must appear at the
    start of a message the model sees. Placing it at the very top of the
    system prompt covers every turn without needing per-message injection.
    """
    msg = base_message or "You are a helpful assistant."
    if calendar_enabled:
        msg = msg + _calendar_suffix(timezone_hint)
    if expressive_mode:
        msg = msg + _EXPRESSIVE_PROMPT_SUFFIX
    if multilingual:
        msg = msg + _MULTILINGUAL_PROMPT_SUFFIX
    if has_knowledge_base:
        msg = msg + _RAG_TOOL_PROMPT_SUFFIX
    if call_transfer_enabled:
        msg = msg + _build_call_transfer_suffix(call_transfer_conditions)
    if outbound_followup_enabled:
        msg = msg + _build_followup_suffix(
            topic=outbound_followup_topic,
            ca_name=outbound_followup_ca_name,
            duration=outbound_followup_duration_minutes,
            tz=outbound_followup_timezone or timezone_hint,
        )
    # END_CALL is unconditional — every assistant gets the end_call tool +
    # the farewell-detection instruction. Append LAST so the most recent
    # instruction in the prompt is the call-control one.
    msg = msg + _END_CALL_PROMPT_SUFFIX
    # /nothink — Sarvam-105b reasoning toggle. MUST be the very first token
    # the model sees, before any other instruction. Disables <think>...</think>
    # reasoning blocks (which would add 2-5s of latency per turn). Re-prepend
    # AFTER all suffixes so a future refactor that touches the assembly order
    # can't accidentally bury this token mid-prompt.
    return "/nothink\n\n" + msg


def _coerce_multilingual_mode(v: Any) -> bool:
    """Coerce the assistant's `multilingual` field to a bool. Accepts truthy
    strings ("true","1","yes","on") so the API can pass either."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    return bool(v) if v is not None else False


def _coerce_expressive_mode(v: Any) -> bool:
    """Coerce the assistant's expressive_mode field to a bool. Treats truthy
    strings ("true","1","yes","on") as True so the API can accept either."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"true", "1", "yes", "on"}
    if isinstance(v, (int, float)):
        return bool(v)
    return False


# ── Call-transfer coercions ──────────────────────────────────────────────────
# Exported (no leading-underscore convention break needed — kept _-prefixed for
# consistency with the other coercers; the warmer imports them by name).
_E164_RE = re.compile(r"\+[1-9]\d{1,14}")  # unanchored — use .fullmatch()


def _coerce_call_transfer_enabled(v: Any) -> bool:
    """Truthy bool/str → True. Anything else (incl. missing) → False."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"true", "1", "yes", "on"}
    if isinstance(v, (int, float)):
        return bool(v)
    return False


def _coerce_call_transfer_number(v: Any) -> str:
    """Return the number iff it's a valid E.164 string; else "" (disables transfer)."""
    if isinstance(v, str):
        s = v.strip()
        if _E164_RE.fullmatch(s):
            return s
    return ""


def _coerce_call_transfer_message(v: Any) -> str:
    """Trim; "" if blank (caller falls back to DEFAULT_TRANSFER_MESSAGE)."""
    if isinstance(v, str):
        s = v.strip()
        if s:
            return s
    return ""


def _coerce_call_transfer_conditions(v: Any) -> str:
    """Trim + cap at 500 chars (this text is concatenated into the system prompt)."""
    if isinstance(v, str):
        return v.strip()[:500]
    return ""


# ── Outbound follow-up coercions ─────────────────────────────────────────────
# All read from the assistant doc; defaults are deliberately safe-fallback so
# the suffix renders fine even when fields are partially filled in.
def _coerce_outbound_followup_enabled(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"true", "1", "yes", "on"}
    if isinstance(v, (int, float)):
        return bool(v)
    return False


def _coerce_str_field(v: Any, *, max_len: int) -> str:
    """Generic string coercer used for ca_name / firm_name / template names /
    topic. Trims and caps length — anything else (None, int, dict) → ""."""
    if isinstance(v, str):
        return v.strip()[:max_len]
    return ""


def _coerce_e164_or_blank(v: Any) -> str:
    if isinstance(v, str):
        s = v.strip()
        if _E164_RE.fullmatch(s):
            return s
    return ""


def _coerce_object_id_str(v: Any) -> str:
    """Accept ObjectId, str-ified ObjectId, or "". Anything else → "" so
    is_followup_effective gates correctly."""
    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        if ObjectId.is_valid(s):
            return s
    return ""


def _coerce_positive_int(v: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _coerce_asr_model(v: Any) -> str:
    """Sarvam ASR model whitelist. Default saaras:v3 (broadest language coverage,
    streaming, transcribe mode keeps source language). Stale Deepgram model
    strings (nova-2-phonecall, nova-3, etc.) from pre-migration docs coerce
    silently with a warning."""
    if isinstance(v, str) and v in _SARVAM_ASR_MODELS:
        return v
    if v not in (None, "", _SARVAM_DEFAULT_ASR_MODEL):
        logger.warning("[CONFIG] Coercing unsupported asr_model=%r → %r",
                       v, _SARVAM_DEFAULT_ASR_MODEL)
    return _SARVAM_DEFAULT_ASR_MODEL


def _coerce_asr_mode(v: Any) -> str:
    """Sarvam Saaras v3 mode. Default 'transcribe' (keeps source language).
    Other modes (translate, verbatim, translit, codemix) require explicit
    opt-in. saarika:v2.5 ignores this field at the API level — coercion is
    still applied so the value passed to sarvam.STT() is always valid."""
    if isinstance(v, str) and v in _SARVAM_ASR_MODES:
        return v
    if v not in (None, "", _SARVAM_DEFAULT_ASR_MODE):
        logger.warning("[CONFIG] Coercing unsupported asr_mode=%r → %r",
                       v, _SARVAM_DEFAULT_ASR_MODE)
    return _SARVAM_DEFAULT_ASR_MODE


def _coerce_asr_lang(v: Any) -> str:
    """Sarvam ASR language (BCP-47 India-locale + 'unknown' for auto-detect).
    Tolerates short codes ('hi', 'en', 'multi') by upgrading to the -IN
    variant or to 'unknown'. Stale Deepgram lang codes (en-US, en-GB, etc.)
    coerce to en-IN."""
    if isinstance(v, str):
        s = v.strip()
        if s in _SARVAM_ASR_LANGUAGES:
            return s
        # Deepgram-era "multi" → Sarvam "unknown" (auto-detect).
        if s.lower() == "multi":
            return "unknown"
        # Short code upgrade: 'hi' → 'hi-IN', 'en' → 'en-IN', etc.
        short = s.split("-", 1)[0].lower()
        upgrade = f"{short}-IN"
        if upgrade in _SARVAM_ASR_LANGUAGES:
            return upgrade
        if s:
            logger.warning("[CONFIG] Coercing unsupported asr_language=%r → %r",
                           v, _SARVAM_DEFAULT_ASR_LANGUAGE)
    return _SARVAM_DEFAULT_ASR_LANGUAGE


def _coerce_llm_model(v: Any) -> str:
    """Sarvam LLM model whitelist. Default sarvam-105b (the flagship). Stale
    OpenAI model strings (gpt-4o-mini, gpt-4-turbo, etc.) from pre-migration
    docs coerce silently with a warning."""
    if isinstance(v, str) and v in _SARVAM_LLM_MODELS:
        return v
    if v not in (None, "", _SARVAM_DEFAULT_LLM_MODEL):
        logger.warning("[CONFIG] Coercing unsupported llm_model=%r → %r",
                       v, _SARVAM_DEFAULT_LLM_MODEL)
    return _SARVAM_DEFAULT_LLM_MODEL


def _coerce_tts_provider(v: Any) -> str:
    """The only supported TTS provider is Sarvam. Anything else (incl. legacy
    'elevenlabs' / 'cartesia' values from before the 2026-05-23 migration)
    coerces to 'sarvam' with a warning."""
    if isinstance(v, str) and v.lower() == "sarvam":
        return "sarvam"
    if v not in (None, "", "sarvam"):
        logger.warning("[CONFIG] Coercing unsupported tts_provider=%r → 'sarvam'", v)
    return "sarvam"


def _coerce_tts_model(v: Any, provider: str = "sarvam") -> str:
    """Sarvam Bulbul model whitelist. Default bulbul:v2 (avoids the v3
    pitch/loudness plugin bug). v3 / v3-beta are accepted for explicit opt-in
    once a workaround is in place. Old ElevenLabs / Cartesia model strings
    (eleven_flash_v2_5, sonic-3, …) from pre-migration docs coerce silently."""
    if isinstance(v, str) and v in _SARVAM_TTS_MODELS:
        return v
    if v not in (None, "", _SARVAM_DEFAULT_MODEL):
        logger.warning("[CONFIG] Coercing unsupported tts_model=%r → %r",
                       v, _SARVAM_DEFAULT_MODEL)
    return _SARVAM_DEFAULT_MODEL


# Audio-cue tags ([laughs], [sneezes], [whispers], …) — legacy ElevenLabs v3
# expressive markup. Sarvam Bulbul has no equivalent and would speak the
# bracketed text literally ("open bracket laughs close bracket"). Strip them
# from greetings defensively so old assistant docs that carry these tags
# don't sound broken.
_AUDIO_TAG_RE = re.compile(r"\[[a-z][a-z _-]{0,30}\]")


def _strip_audio_cue_tags(text: Any) -> Any:
    if not isinstance(text, str) or "[" not in text:
        return text
    cleaned = _AUDIO_TAG_RE.sub("", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if cleaned and cleaned != text:
        logger.warning("[CONFIG] stripped legacy audio-cue tag(s) from greeting "
                       "(Sarvam Bulbul would read them aloud)")
        return cleaned
    return text


def _coerce_tts_voice(v: Any, provider: str = "sarvam", model: str = _SARVAM_DEFAULT_MODEL) -> str:
    """Sarvam Bulbul speaker, validated against the target model's compatibility
    list (livekit-plugins-sarvam enforces this strictly — sending an incompatible
    speaker raises ValueError on TTS init, which would crash every agent job).

    Per-model defaults:
      - bulbul:v2 → 'anushka' (female Indian-English)
      - bulbul:v3 / v3-beta → 'shubh' (male Hindi, plugin's own v3 default)

    Coercion rules:
      - Unknown / legacy values (ElevenLabs 20-char IDs, Cartesia UUIDs,
        OpenAI 'alloy', etc.) → model-appropriate default.
      - v2-only speaker (e.g. 'anushka') + model=v3 → downgrade to 'shubh'
        (anushka does NOT work on v3, confirmed end-to-end).
      - v3-only speaker (e.g. 'pooja') + model=v2 → downgrade to 'anushka'.
    """
    # Pick the compatibility set + per-model default.
    if model in ("bulbul:v3", "bulbul:v3-beta"):
        compatible = _SARVAM_V3_SPEAKERS
        model_default = "shubh"
    elif model == "bulbul:v2":
        compatible = _SARVAM_V2_SPEAKERS
        model_default = "anushka"
    else:
        # Unknown model — fall back to the union set and the locked default.
        compatible = _SARVAM_TTS_SPEAKERS
        model_default = _SARVAM_DEFAULT_SPEAKER

    if isinstance(v, str) and v in compatible:
        return v
    # Speaker is either unknown entirely or known-but-incompatible with this
    # model. Distinguish so the warning is informative.
    if isinstance(v, str) and v in _SARVAM_TTS_SPEAKERS:
        logger.warning("[CONFIG] Speaker %r is incompatible with model=%r — "
                       "downgrading to %r", v, model, model_default)
    elif v not in (None, "", model_default):
        logger.warning("[CONFIG] Coercing unsupported tts_voice=%r → %r (model=%r)",
                       v, model_default, model)
    return model_default


def _coerce_tts_language(v: Any) -> str:
    """Sarvam Bulbul language code (BCP-47, India-locale). Default 'en-IN'.

    Accepts the 11 codes Bulbul supports (bn-IN, en-IN, gu-IN, hi-IN, kn-IN,
    ml-IN, mr-IN, od-IN, pa-IN, ta-IN, te-IN). Tolerates short codes ('hi',
    'en') by upgrading to the -IN variant. Old Cartesia short codes (e.g. 'es',
    'fr') warn and coerce to en-IN."""
    if isinstance(v, str):
        s = v.strip()
        if s in _SARVAM_TTS_LANGUAGES:
            return s
        # Tolerate bare short codes — Bulbul only does Indian languages so
        # we upgrade 'hi' → 'hi-IN', 'en' → 'en-IN', etc.
        short = s.split("-", 1)[0].lower()
        upgrade = f"{short}-IN"
        if upgrade in _SARVAM_TTS_LANGUAGES:
            return upgrade
        if s:
            logger.warning("[CONFIG] Coercing unsupported tts_language=%r → %r",
                           v, _SARVAM_DEFAULT_LANGUAGE)
    return _SARVAM_DEFAULT_LANGUAGE


def _coerce_tts_emotion(v: Any) -> list:
    """No-op coercer. Sarvam Bulbul has no emotion-control parameter; any
    value stored on an assistant doc from the Cartesia era is dropped. Kept
    as a function (returning []) so call sites and Mongo schema are stable
    until we get round to removing the field entirely."""
    if v not in (None, "", [], ()):
        logger.debug("[CONFIG] Dropping legacy tts_emotion=%r (Sarvam Bulbul has no "
                     "emotion control)", v)
    return []


def load_assistant_config(assistant_id: str) -> Dict[str, Any]:
    """Read assistant + calendar config from Mongo and flatten into a dict the
    agent worker can consume. Raises ValueError if the assistant doesn't exist."""
    db = Database.get_db()
    try:
        assistant_obj_id = ObjectId(assistant_id)
    except Exception as exc:
        raise ValueError(f"Invalid assistant_id: {assistant_id}") from exc

    assistant = db["assistants"].find_one({"_id": assistant_obj_id})
    if not assistant:
        raise ValueError(f"Assistant not found: {assistant_id}")

    assistant_user_id = assistant.get("user_id")

    timezone_hint = (
        assistant.get("timezone")
        or settings.default_timezone
        or "America/New_York"
    )

    calendar_enabled = False
    calendar_account_ids_list: list[str] = []
    calendar_account_id_for_booking: Optional[ObjectId] = None
    default_calendar_provider = "google"

    calendar_accounts = db["calendar_accounts"]
    assistant_calendar_ids = assistant.get("calendar_account_ids", [])
    assistant_calendar_enabled = assistant.get("calendar_enabled", False)

    if assistant_calendar_ids and assistant_calendar_enabled and assistant_user_id:
        for cal_id in assistant_calendar_ids:
            if calendar_accounts.find_one({"_id": cal_id, "user_id": assistant_user_id}):
                calendar_account_ids_list.append(str(cal_id))
        if calendar_account_ids_list:
            calendar_enabled = True

    if not calendar_enabled and assistant.get("calendar_account_id"):
        cal_id = assistant["calendar_account_id"]
        account_doc = calendar_accounts.find_one({"_id": cal_id})
        if account_doc:
            calendar_enabled = True
            calendar_account_id_for_booking = cal_id
            calendar_account_ids_list = [str(cal_id)]
            default_calendar_provider = account_doc.get("provider", "google")

    # TTS provider chosen per-assistant. Coerce first, then use it to pick the
    # right model/voice whitelist (each provider has its own namespaces).
    tts_provider = _coerce_tts_provider(assistant.get("tts_provider"))
    raw_voice = assistant.get("tts_voice") or assistant.get("voice")

    # Expressive mode: opt-in. Appends an emotion-tag + natural-filler prompt
    # suffix so the LLM responds more conversationally.
    expressive_mode = _coerce_expressive_mode(assistant.get("expressive_mode"))
    tts_model = _coerce_tts_model(assistant.get("tts_model"), tts_provider)

    # Multilingual mode: forces ASR to Sarvam Saaras v3 with language="unknown"
    # so Sarvam auto-detects per utterance across all 22 supported Indic
    # languages plus en-IN. Saaras v3 specifically supports code-switching
    # (caller flips Hindi ↔ English mid-utterance). When OFF, the configured
    # language (e.g. "hi-IN") is pinned for tighter accuracy.
    multilingual_mode = _coerce_multilingual_mode(assistant.get("multilingual"))
    if multilingual_mode:
        asr_model_resolved = "saaras:v3"
        asr_lang_resolved = "unknown"
    else:
        asr_model_resolved = _coerce_asr_model(assistant.get("asr_model"))
        asr_lang_resolved = _coerce_asr_lang(assistant.get("asr_language"))
    asr_mode_resolved = _coerce_asr_mode(assistant.get("asr_mode"))

    # Call transfer to a human agent: opt-in per assistant. The "effective"
    # flag requires BOTH the toggle AND a valid E.164 number — so the prompt
    # suffix and the transfer_to_agent tool never get exposed when there's no
    # number to transfer to (the LLM would just offer something that no-ops).
    call_transfer_enabled_raw = _coerce_call_transfer_enabled(assistant.get("call_transfer_enabled"))
    call_transfer_number = _coerce_call_transfer_number(assistant.get("call_transfer_number"))
    call_transfer_message = _coerce_call_transfer_message(assistant.get("call_transfer_message")) or DEFAULT_TRANSFER_MESSAGE
    call_transfer_conditions = _coerce_call_transfer_conditions(assistant.get("call_transfer_conditions"))
    call_transfer_effective = bool(call_transfer_enabled_raw and call_transfer_number)

    # Outbound follow-up workflow (tax-attorney-style flow). Effective only
    # when the flag AND the minimum bookable-state config are present —
    # mirrors the call-transfer pattern. Anything missing → suffix not
    # appended, function-tool wrappers no-op.
    followup_enabled_raw = _coerce_outbound_followup_enabled(assistant.get("outbound_followup_enabled"))
    followup_topic = _coerce_str_field(assistant.get("outbound_followup_topic"), max_len=60)
    followup_ca_name = _coerce_str_field(assistant.get("ca_name"), max_len=80)
    followup_ca_phone = _coerce_e164_or_blank(assistant.get("ca_phone"))
    followup_ca_calendar = _coerce_object_id_str(assistant.get("ca_calendar_account_id"))
    followup_firm_name = _coerce_str_field(assistant.get("firm_name"), max_len=80)
    followup_wa_client = _coerce_str_field(assistant.get("wa_template_client"), max_len=80)
    followup_wa_ca = _coerce_str_field(assistant.get("wa_template_ca"), max_len=80)
    followup_duration = _coerce_positive_int(
        assistant.get("appointment_duration_minutes"), default=30, lo=5, hi=240,
    )
    followup_tz = _coerce_str_field(assistant.get("appointment_timezone"), max_len=64)
    followup_effective = bool(
        followup_enabled_raw
        and followup_ca_calendar
        and followup_ca_phone
        and (followup_wa_client or followup_wa_ca)
    )

    # Build the FULL system message via the shared helper. This is the SINGLE
    # source of truth — the cache warmer also calls build_system_message with
    # the same inputs so OpenAI's prompt cache hits 100% of the time.
    has_knowledge_base = bool(assistant.get("knowledge_base_files"))
    system_message = build_system_message(
        base_message=assistant.get("system_message", "You are a helpful assistant."),
        calendar_enabled=calendar_enabled,
        timezone_hint=timezone_hint,
        expressive_mode=expressive_mode,
        multilingual=multilingual_mode,
        has_knowledge_base=has_knowledge_base,
        call_transfer_enabled=call_transfer_effective,
        call_transfer_conditions=call_transfer_conditions,
        outbound_followup_enabled=followup_effective,
        outbound_followup_topic=followup_topic,
        outbound_followup_ca_name=followup_ca_name,
        outbound_followup_duration_minutes=followup_duration,
        outbound_followup_timezone=followup_tz,
    )

    return {
        "assistant_id": str(assistant["_id"]),
        "user_id": str(assistant_user_id) if assistant_user_id else None,
        "name": assistant.get("name", "Assistant"),
        "system_message": system_message,
        "greeting": _strip_audio_cue_tags(assistant.get("call_greeting") or "Hello! How can I help you today?"),
        # voice/tts/asr/llm are coerced to the locked Convis stack — see module docstring.
        "tts_provider": tts_provider,
        "voice": _coerce_tts_voice(raw_voice, tts_provider, tts_model),
        "tts_voice": _coerce_tts_voice(raw_voice, tts_provider, tts_model),
        "tts_model": tts_model,
        "expressive_mode": expressive_mode,
        "multilingual": multilingual_mode,
        # Legacy ElevenLabs voice_settings (stability / similarity_boost / style).
        # Sarvam Bulbul ignores these — the agent worker no longer reads them
        # — but we keep them in the returned dict so the frontend's
        # assistant-edit form doesn't choke on a missing field. Safe to remove
        # once the frontend is migrated to Sarvam-native controls.
        "tts_stability": assistant.get("tts_stability", 0.5),
        "tts_similarity_boost": assistant.get("tts_similarity_boost", 0.75),
        "tts_style": assistant.get("tts_style", 0.0),
        # Sarvam Bulbul's pace control. Maps to the `pace` kwarg in
        # sarvam.TTS(). 1.0 is natural speed; 0.5–1.5 is the useful range.
        "tts_speed": assistant.get("tts_speed", 1.0),
        # Sarvam Bulbul language (BCP-47, India-locale). Default en-IN.
        "tts_language": _coerce_tts_language(assistant.get("tts_language")),
        # Legacy Cartesia emotion field — now a no-op. Sarvam Bulbul has no
        # emotion control. Kept in the dict so Mongo schema / frontend shape
        # stay stable; coerces to [] regardless of what's stored.
        "tts_emotion": _coerce_tts_emotion(assistant.get("tts_emotion")),
        # Per-assistant turn-detection knobs. Without these, agent_worker.py
        # falls back to its DEFAULT_MIN_* constants regardless of what's
        # stored in Mongo.
        "min_interruption_duration": assistant.get("min_interruption_duration"),
        "min_endpointing_delay": assistant.get("min_endpointing_delay"),
        "asr_endpointing_ms": assistant.get("asr_endpointing_ms"),
        "asr_model": asr_model_resolved,
        "asr_mode": asr_mode_resolved,
        "asr_language": asr_lang_resolved,
        # Legacy Deepgram keyword-biasing field. Sarvam Saaras has no
        # equivalent; the field is no-op'd at the agent worker but kept in
        # the dict so the API response shape doesn't change.
        "asr_keywords": assistant.get("asr_keywords", []),
        "llm_model": _coerce_llm_model(assistant.get("llm_model")),
        "llm_max_tokens": assistant.get("llm_max_tokens", 250),
        "temperature": assistant.get("temperature", 0.7),
        "bot_language": assistant.get("bot_language", "en"),
        "calendar_enabled": calendar_enabled,
        "calendar_account_ids": calendar_account_ids_list,
        "calendar_account_id": (
            str(calendar_account_id_for_booking) if calendar_account_id_for_booking else None
        ),
        "calendar_provider": default_calendar_provider,
        "timezone": timezone_hint,
        "tools_enabled": assistant.get("tools_enabled", False),
        "tools": assistant.get("tools", []),
        # Surface KB presence here (cheap — already in the assistant doc) so
        # the agent worker doesn't need an extra Mongo round-trip on every
        # call start to figure out whether to wire up the RAG tool prompt.
        "has_knowledge_base": bool(assistant.get("knowledge_base_files")),
        # Call transfer to a human. `call_transfer_enabled` here is the
        # EFFECTIVE flag (toggle AND valid number) — the agent's
        # transfer_to_agent tool gates on it. `call_transfer_message` is
        # already resolved (configured value or the default).
        "call_transfer_enabled": call_transfer_effective,
        "call_transfer_number": call_transfer_number,
        "call_transfer_message": call_transfer_message,
        "call_transfer_conditions": call_transfer_conditions,
        # Conversation memory across calls. Surface the raw Mongo value
        # unchanged — the agent's entrypoint reads these to decide whether
        # to build a contact_history_block. Defaults to OFF / 3 calls so
        # assistants that never enabled this feature behave exactly as
        # before. NOT passed to build_system_message: conversation history
        # is injected as a SEPARATE chat_ctx message (cache-preserving),
        # not appended to system_message.
        "conversation_history_enabled": bool(assistant.get("conversation_history_enabled") or False),
        "conversation_history_max_calls": max(1, min(int(assistant.get("conversation_history_max_calls") or 3), 10)),
        # Outbound follow-up workflow. `outbound_followup_enabled` here is the
        # EFFECTIVE flag — both function tools (and is_followup_effective)
        # re-check the same gates defensively.
        "outbound_followup_enabled": followup_effective,
        "outbound_followup_topic": followup_topic,
        "ca_name": followup_ca_name,
        "ca_phone": followup_ca_phone,
        "ca_calendar_account_id": followup_ca_calendar,
        "firm_name": followup_firm_name,
        "wa_template_client": followup_wa_client,
        "wa_template_ca": followup_wa_ca,
        "appointment_duration_minutes": followup_duration,
        "appointment_timezone": followup_tz or timezone_hint,
    }


def encode_metadata(config: Dict[str, Any]) -> str:
    """JSON-encode config for LiveKit room metadata (max ~64KB)."""
    return json.dumps(config, default=str)


def decode_metadata(metadata: str) -> Dict[str, Any]:
    return json.loads(metadata) if metadata else {}
