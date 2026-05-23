"""Load an assistant from MongoDB into the runtime config used by the LiveKit agent.

Shared by the FastAPI process (for browser token dispatch) and the agent worker
process (which reads room metadata to find the assistant_id, then calls this).

Convis shares its MongoDB with other unrelated projects that store assistants
with different ASR/LLM/TTS provider values (Qwen, Sarvam, Piper, Whisper, etc).
This loader coerces any non-supported value back to the locked Convis stack —
Deepgram ASR + OpenAI LLM + ElevenLabs TTS — so a foreign project's writes can
never crash a Convis call (e.g. agent sending model=Qwen/... to Deepgram → 403).
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
_DEEPGRAM_ASR_MODELS = {
    "nova-3", "nova-2", "nova-2-general", "nova-2-meeting",
    "nova-2-phonecall", "nova-2-finance", "nova-2-conversationalai",
    "nova-2-voicemail", "nova-2-video", "nova-2-medical", "nova-2-drivethru",
    "enhanced", "enhanced-general", "enhanced-meeting", "enhanced-phonecall",
    "base", "base-general", "base-meeting", "base-phonecall",
}
_DEEPGRAM_LANGS = {
    "en", "en-US", "en-GB", "en-AU", "en-IN", "en-NZ", "multi",
    "es", "es-419", "fr", "fr-CA", "de", "hi", "hi-Latn", "it", "ja", "ko",
    "nl", "pt", "pt-BR", "ru", "zh", "zh-CN", "zh-TW", "tr", "uk", "id", "vi",
    "th", "pl", "sv", "da", "fi", "el", "ar", "he", "cs", "ro", "no", "ms", "ta",
}
_OPENAI_LLM_MODELS = {
    "gpt-4o", "gpt-4o-mini", "gpt-4o-2024-11-20", "gpt-4o-2024-08-06",
    "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo",
    "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
}
_ELEVEN_TTS_MODELS = {
    "eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_multilingual_v2",
    "eleven_monolingual_v1", "eleven_flash_v2", "eleven_turbo_v2",
    "eleven_v3",
}
# ElevenLabs voice IDs are 20-char alphanumeric (e.g. "21m00Tcm4TlvDq8ikWAM" = Rachel).
# Anything else ("alloy", "anushka", "en_US-lessac-medium") is from a foreign stack.
_ELEVEN_VOICE_RE = re.compile(r"^[A-Za-z0-9]{20}$")
_RACHEL_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

# Cartesia Sonic — alternate TTS provider, ~5× cheaper than ElevenLabs Flash.
# Voice IDs are UUIDs.
#
# sonic-3 is Cartesia's current flagship streaming model and is the plugin
# default. We default to it explicitly so a future plugin upgrade (which may
# change the default again) doesn't silently shift our users.
_CARTESIA_TTS_MODELS = {
    "sonic-3",
    "sonic-2",          # previous-gen; still supported, kept for explicit pinning
    "sonic",
    "sonic-lite",
    "sonic-preview",
    "sonic-turbo",
    "sonic-english",
    "sonic-multilingual",
}
_CARTESIA_DEFAULT_MODEL = "sonic-3"
# UUIDs are case-insensitive per RFC 4122. Cartesia returns lowercase from
# their API but accepts both — and many of their docs/examples use uppercase.
_CARTESIA_VOICE_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Default Cartesia voice — "Maya" is a clear, neutral female voice ideal for
# customer service. Override per-assistant with a UUID from Cartesia voice library.
_CARTESIA_DEFAULT_VOICE = "694f9389-aac1-45b6-b726-9d9369183238"

# Cartesia-native knobs. These are NOT mirrored to ElevenLabs — each provider
# gets its own native tuning surface.
#
# Emotions: sonic-3 uses Cartesia's "generation_config" path which takes a
# SINGLE Title-Case emotion string. The full Literal enum from the plugin
# (livekit/plugins/cartesia/models.py::TTSVoiceEmotion) is mirrored below so
# any value the plugin accepts also passes our whitelist. (Sonic-2's legacy
# "__experimental_controls" path used lowercase <name>:<level> tokens; that
# format is dead in sonic-3 and we deliberately don't support it — pinning
# to legacy api_version would lock us to a sunsetting Cartesia model.)
_CARTESIA_EMOTION_NAMES = {
    "Happy", "Excited", "Enthusiastic", "Elated", "Euphoric", "Triumphant",
    "Amazed", "Surprised", "Flirtatious", "Joking/Comedic", "Curious",
    "Content", "Peaceful", "Serene", "Calm", "Grateful", "Affectionate",
    "Trust", "Sympathetic", "Anticipation", "Mysterious", "Angry", "Mad",
    "Outraged", "Frustrated", "Agitated", "Threatened", "Disgusted",
    "Contempt", "Envious", "Sarcastic", "Ironic", "Sad", "Dejected",
    "Melancholic", "Disappointed", "Hurt", "Guilty", "Bored", "Tired",
    "Rejected", "Nostalgic", "Wistful", "Apologetic", "Hesitant", "Insecure",
    "Confused", "Resigned", "Anxious", "Panicked", "Alarmed", "Scared",
    "Neutral", "Proud", "Confident", "Distant", "Skeptical", "Contemplative",
    "Determined",
}
# Case-folded lookup for tolerant matching ("happy" / "HAPPY" / "Happy" all
# resolve to "Happy"). Built once at module load.
_CARTESIA_EMOTION_LOOKUP = {e.lower(): e for e in _CARTESIA_EMOTION_NAMES}
# Sonic-3 supports a wide set; whitelist the BCP-47 short codes Cartesia
# actually accepts (one-to-one with their /voices language tags).
_CARTESIA_LANGUAGES = {
    "en", "es", "fr", "de", "it", "pt", "pl", "ru", "nl", "tr", "ja", "ko",
    "zh", "hi", "id", "sv", "cs", "ar", "ro", "hu",
}

_TTS_PROVIDERS = {"elevenlabs", "cartesia"}


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


# Multilingual mode: opt-in. When enabled, ASR runs in language=multi (auto-
# detect per utterance across 30+ languages) and the LLM is instructed to
# match the caller's language. ElevenLabs Flash v2.5 already speaks 32
# languages from the same voice ID, so no TTS change is needed.
#
# Trade-off: switching ASR off `nova-2-phonecall` (English+PSTN-tuned) onto
# `nova-2` (general multilingual) loses some accuracy on noisy 8 kHz English
# phone audio. Acceptable for multilingual use cases; do not enable for
# English-only customers chasing best PSTN WER.
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
    return msg


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
    if isinstance(v, str) and v in _DEEPGRAM_ASR_MODELS:
        return v
    if v not in (None, "", "nova-2"):
        logger.warning("[CONFIG] Coercing unsupported asr_model=%r → 'nova-2'", v)
    return "nova-2"


def _coerce_asr_lang(v: Any) -> str:
    if isinstance(v, str) and v in _DEEPGRAM_LANGS:
        return v
    if v not in (None, "", "en"):
        logger.warning("[CONFIG] Coercing unsupported asr_language=%r → 'en'", v)
    return "en"


def _coerce_llm_model(v: Any) -> str:
    if isinstance(v, str) and v in _OPENAI_LLM_MODELS:
        return v
    if v not in (None, "", "gpt-4o-mini"):
        logger.warning("[CONFIG] Coercing unsupported llm_model=%r → 'gpt-4o-mini'", v)
    return "gpt-4o-mini"


def _coerce_tts_provider(v: Any) -> str:
    """Default to ElevenLabs (existing prod path). Cartesia is opt-in per assistant."""
    if isinstance(v, str) and v.lower() in _TTS_PROVIDERS:
        return v.lower()
    if v not in (None, "", "elevenlabs"):
        logger.warning("[CONFIG] Coercing unsupported tts_provider=%r → 'elevenlabs'", v)
    return "elevenlabs"


def _coerce_tts_model(v: Any, provider: str = "elevenlabs") -> str:
    """Pick a model whitelist based on TTS provider. ElevenLabs and Cartesia
    use entirely different model namespaces — eleven_flash_v2_5 vs sonic-3.

    Cartesia default is sonic-3 (current flagship, supports the modern
    generation_config.emotion path).

    Auto-upgrade: legacy sonic-2 / sonic / sonic-english / sonic-multilingual
    / sonic-turbo are RUNTIME-FORCED to sonic-3 here. The historical reason:
    those assistants stored their model by platform default (the UI never
    exposed a model picker), not by deliberate user choice. Forcing sonic-3
    gives them the modern emotion controls + better quality without anyone
    having to re-save the assistant. sonic-lite / sonic-preview are also
    upgraded for the same reason. Mongo docs may still have stale values;
    that's fine — the coercion overrides at every agent load."""
    if provider == "cartesia":
        if isinstance(v, str) and v in _CARTESIA_TTS_MODELS:
            # Auto-upgrade ANY legacy sonic-* to sonic-3.
            # The whitelist still includes them for forward-compat (e.g. a
            # future admin tool that explicitly wants to pin an older model
            # could re-route through a different path), but the live agent
            # always serves sonic-3.
            if v != _CARTESIA_DEFAULT_MODEL:
                logger.info(
                    "[CONFIG] Auto-upgrading legacy Cartesia tts_model=%r → %r "
                    "(historical platform default, not a deliberate pin)",
                    v, _CARTESIA_DEFAULT_MODEL,
                )
                return _CARTESIA_DEFAULT_MODEL
            return v
        if v not in (None, "", _CARTESIA_DEFAULT_MODEL):
            logger.warning("[CONFIG] Coercing Cartesia tts_model=%r → %r",
                           v, _CARTESIA_DEFAULT_MODEL)
        return _CARTESIA_DEFAULT_MODEL
    # default: ElevenLabs
    if isinstance(v, str) and v in _ELEVEN_TTS_MODELS:
        return v
    if v not in (None, "", "eleven_flash_v2_5"):
        logger.warning("[CONFIG] Coercing unsupported tts_model=%r → 'eleven_flash_v2_5'", v)
    return "eleven_flash_v2_5"


# ElevenLabs v3 audio-cue tags ([laughs], [sneezes], [whispers], …). They only
# do anything on the v3 model — which we never run (coerced to flash_v2_5). On
# flash they'd be SPOKEN LITERALLY ("open bracket laughs close bracket"), so
# strip them from greetings defensively. Conservative pattern: a short
# lowercase-word(s) token in square brackets.
_AUDIO_TAG_RE = re.compile(r"\[[a-z][a-z _-]{0,30}\]")


def _strip_audio_cue_tags(text: Any) -> Any:
    if not isinstance(text, str) or "[" not in text:
        return text
    cleaned = _AUDIO_TAG_RE.sub("", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if cleaned and cleaned != text:
        logger.warning("[CONFIG] stripped ElevenLabs-v3 audio-cue tag(s) from greeting "
                       "(flash_v2_5 would read them aloud)")
        return cleaned
    return text


def _coerce_tts_voice(v: Any, provider: str = "elevenlabs") -> str:
    """Voice ID format depends on provider — ElevenLabs uses 20-char alnum,
    Cartesia uses UUID. A voice from one provider is not valid for the other."""
    if provider == "cartesia":
        if isinstance(v, str) and _CARTESIA_VOICE_RE.match(v):
            return v
        if v not in (None, "", _CARTESIA_DEFAULT_VOICE):
            logger.warning("[CONFIG] Coercing non-Cartesia tts_voice=%r → default", v)
        return _CARTESIA_DEFAULT_VOICE
    # default: ElevenLabs
    if isinstance(v, str) and _ELEVEN_VOICE_RE.match(v):
        return v
    if v not in (None, "", _RACHEL_VOICE_ID):
        logger.warning("[CONFIG] Coercing non-ElevenLabs tts_voice=%r → Rachel", v)
    return _RACHEL_VOICE_ID


def _coerce_tts_language(v: Any) -> str:
    """Cartesia-only knob. BCP-47 short code from the whitelist; else 'en'.
    ElevenLabs ignores this — its model handles language detection per voice."""
    if isinstance(v, str):
        s = v.strip().lower()
        # Accept "en-US" → "en" (Cartesia uses short codes only).
        s = s.split("-", 1)[0]
        if s in _CARTESIA_LANGUAGES:
            return s
        if s:
            logger.warning("[CONFIG] Coercing unsupported tts_language=%r → 'en'", v)
    return "en"


def _coerce_tts_emotion(v: Any) -> list:
    """Cartesia (sonic-3) takes a SINGLE emotion value drawn from its Title-Case
    Literal enum (Happy / Curious / Determined / Calm / …). We store the field
    as a list for Mongo / Pydantic stability (forward-compat with future
    multi-emotion APIs and zero-migration for the [] default), but the list
    always has zero or one element after coercion.

    Accepts: a string ("happy" / "HAPPY" / "Happy" — all resolve to "Happy"),
    or a list/tuple (we pick the first valid value), or empty/None ([] = no
    emotion). Anything else → [] (Cartesia falls back to its natural prosody,
    no crash, no API error).

    NOTE: ANY legacy ":level" suffix from the pre-sonic-3 emotion format is
    stripped silently — sonic-3 doesn't understand "<name>:<level>" and would
    400 on it. Surfacing that as a warning so we can spot stale Mongo docs.
    """
    if v is None or v == "" or v == [] or v == ():
        return []
    # Normalize to a list of string tokens to scan
    if isinstance(v, str):
        tokens = [t.strip() for t in v.replace(";", ",").split(",") if t.strip()]
    elif isinstance(v, (list, tuple)):
        tokens = [str(t).strip() for t in v if str(t).strip()]
    else:
        return []

    for tok in tokens:
        # Strip legacy ":level" suffix from the old sonic-2 experimental_controls
        # syntax — sonic-3 rejects it.
        if ":" in tok:
            logger.warning(
                "[CONFIG] Stripping legacy ':level' suffix from Cartesia emotion=%r "
                "(sonic-3 uses single-value emotion, not <name>:<level>)", tok,
            )
            tok = tok.split(":", 1)[0]
        canonical = _CARTESIA_EMOTION_LOOKUP.get(tok.lower())
        if not canonical:
            logger.warning("[CONFIG] Dropping unknown Cartesia emotion=%r", tok)
            continue
        # First valid match wins; sonic-3 takes one.
        return [canonical]
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
    # Defensive: if any assistant got eleven_v3 stored (from the brief
    # window where we forced it), coerce back to flash so calls don't 403.
    if tts_model == "eleven_v3":
        logger.warning(
            "[CONFIG] Coercing tts_model=eleven_v3 → eleven_flash_v2_5 "
            "(v3 not supported by livekit-plugins-elevenlabs streaming WSS)"
        )
        tts_model = "eleven_flash_v2_5"

    # Multilingual mode: forces ASR to nova-3 + language=multi. nova-3
    # specifically supports code-switching (caller flips between languages
    # mid-utterance) with materially better accuracy than nova-2's "multi".
    # The single "multi" language code tells Deepgram to auto-detect per
    # utterance — no `detect_language=True` is needed (and using both can
    # actually reduce code-switching quality).
    multilingual_mode = _coerce_multilingual_mode(assistant.get("multilingual"))
    if multilingual_mode:
        asr_model_resolved = "nova-3"
        asr_lang_resolved = "multi"
    else:
        asr_model_resolved = _coerce_asr_model(assistant.get("asr_model"))
        asr_lang_resolved = _coerce_asr_lang(assistant.get("asr_language"))

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
        "voice": _coerce_tts_voice(raw_voice, tts_provider),
        "tts_voice": _coerce_tts_voice(raw_voice, tts_provider),
        "tts_model": tts_model,
        "expressive_mode": expressive_mode,
        "multilingual": multilingual_mode,
        "tts_stability": assistant.get("tts_stability", 0.5),
        "tts_similarity_boost": assistant.get("tts_similarity_boost", 0.75),
        "tts_style": assistant.get("tts_style", 0.0),
        "tts_speed": assistant.get("tts_speed", 1.0),
        # Cartesia-only knobs. Agent worker reads these only when
        # tts_provider == "cartesia"; ElevenLabs path ignores them. Coerced
        # here so an invalid emotion / language can't reach the Cartesia API
        # mid-call (would 400 and drop the TTS WSS).
        "tts_language": _coerce_tts_language(assistant.get("tts_language")),
        "tts_emotion": _coerce_tts_emotion(assistant.get("tts_emotion")),
        # Per-assistant turn-detection knobs. Without these, agent_worker.py
        # falls back to its DEFAULT_MIN_* constants regardless of what's
        # stored in Mongo.
        "min_interruption_duration": assistant.get("min_interruption_duration"),
        "min_endpointing_delay": assistant.get("min_endpointing_delay"),
        "asr_endpointing_ms": assistant.get("asr_endpointing_ms"),
        "asr_model": asr_model_resolved,
        "asr_language": asr_lang_resolved,
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
