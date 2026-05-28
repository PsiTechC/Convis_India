"""LiveKit Agent worker — the entrypoint process that joins rooms and runs
the Deepgram → Sarvam-105b → Sarvam Bulbul voice pipeline. (Phase 2 will move
ASR to Sarvam Saaras v3 mode=transcribe.)

Runs as its own process. Start with:
    python -m app.services.livekit.agent_worker start
    python -m app.services.livekit.agent_worker dev   # for hot reload

The worker connects to LiveKit Cloud using LIVEKIT_URL / _API_KEY / _API_SECRET
and registers under LIVEKIT_AGENT_NAME. Rooms created via agent_dispatch get
routed here. Room metadata carries the assistant config (see assistant_config.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Load .env into os.environ BEFORE any livekit/sarvam/deepgram import. The
# LiveKit Agents CLI reads LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
# directly from os.environ (not via pydantic-settings), and the Sarvam plugin
# reads SARVAM_API_KEY the same way. Without this, running the agent worker
# locally (`python -m app.services.livekit.agent_worker dev`) raises:
#     ValueError: ws_url is required, or set LIVEKIT_URL environment variable
# In production (App Runner / ECS), env vars are injected by the task
# definition so this is a no-op — load_dotenv silently skips when no file
# exists. .env lives at convis-api/.env (3 parents up from this file).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
except ImportError:
    # python-dotenv is in requirements.txt; missing → some other startup bug.
    pass

from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, ChatContext, JobContext, WorkerOptions, cli, function_tool
from livekit.plugins import sarvam, silero

from app.services.livekit.assistant_config import (
    decode_metadata,
    load_assistant_config,
)

logger = logging.getLogger("convis.livekit.agent")
logging.basicConfig(level=logging.INFO)


def _resolve_config(ctx: JobContext) -> Dict[str, Any]:
    """Pull the assistant config out of job/room metadata.

    Three resolution paths, in order of preference:
    1. metadata already contains system_message → use as-is (outbound calls
       built our config).
    2. metadata contains assistant_id → DB lookup by id (browser-side flows).
    3. metadata is empty / SIP inbound → derive the dialed number from room
       name or SIP attributes, look up the assigned assistant in phone_numbers.
       This is how Vobiz inbound works: LiveKit's Callee dispatch rule names
       the room after the To: number; we resolve the assistant from there.
    """
    meta: Dict[str, Any] = {}
    if ctx.job.metadata:
        try:
            meta = decode_metadata(ctx.job.metadata)
        except json.JSONDecodeError:
            logger.warning("Job metadata is not valid JSON, ignoring")
    if not meta and ctx.room.metadata:
        try:
            meta = decode_metadata(ctx.room.metadata)
        except json.JSONDecodeError:
            logger.warning("Room metadata is not valid JSON, ignoring")

    if "system_message" not in meta and meta.get("assistant_id"):
        meta = {**load_assistant_config(meta["assistant_id"]), **meta}

    # Inbound-from-SIP path: derive dialed number from room name and look up
    # which assistant is assigned. LiveKit's Callee dispatch rule names the
    # room after the To: header user-part (or <prefix><number> if prefix set).
    #
    # SNAPSHOT: once we resolve an assistant_id from the dialed number, write
    # it back into the room metadata in LiveKit. If the user reassigns the
    # number mid-call, the next agent-side lookup (e.g. on a worker restart
    # after a crash) sees the snapshot and serves the SAME assistant the
    # caller started talking to. Without this, a customer in turn 5 of a
    # conversation could suddenly hear a different assistant if the user
    # toggled assignment in the dashboard mid-call.
    if "system_message" not in meta:
        # If a previous resolver run already snapshotted assistant_id into the
        # room metadata, prefer that — never re-derive from the dialed number
        # if we have a snapshot.
        snapshot_id = meta.get("assistant_id")
        if snapshot_id:
            meta = {**load_assistant_config(snapshot_id), **meta}
            logger.info("[AGENT] Inbound from snapshot: assistant_id=%s", snapshot_id)
        else:
            dialed = _extract_dialed_number(ctx.room.name, meta)
            if dialed:
                assistant_id = _lookup_assistant_for_number(dialed)
                if assistant_id:
                    meta = {
                        **load_assistant_config(assistant_id),
                        **meta,
                        "assistant_id": assistant_id,   # snapshot for any later resolution
                        "dialed_number": dialed,
                        "direction": "inbound",
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                    }
                    logger.info(
                        "[AGENT] Inbound resolved: dialed=%s → assistant_id=%s (snapshot)",
                        dialed, assistant_id,
                    )

    if "system_message" not in meta:
        raise RuntimeError(
            "No assistant config found in job/room metadata "
            f"(room={ctx.room.name})"
        )

    # A call that's resuming after a failed transfer must NOT be allowed to
    # attempt another transfer — otherwise a permanently-unavailable human
    # number would loop forever (re-provision → AI → transfer → no-answer →
    # re-provision → …), burning Twilio/LLM/TTS minutes each round. "One
    # transfer attempt per original call" is enforced here server-side, not
    # just by the prompt. The prompt suffix may still be present (so the
    # prompt-cache prefix stays identical to the warmer's), but transfer_to_agent
    # will return "transfer not available" and the LLM apologises and continues.
    if meta.get("resumed_after_failed_transfer"):
        meta["call_transfer_enabled"] = False

    return meta


def _extract_dialed_number(room_name: str, meta: Dict[str, Any]) -> str | None:
    """Extract the dialed PSTN number from the room name or metadata.

    LiveKit Callee dispatch creates rooms like "+918065481572" or
    "vobiz-in-+918065481572". We accept any E.164 substring as the dialed
    number. Metadata keys "dialed_number" / "to" / "callee" win if present.
    """
    for key in ("dialed_number", "to", "callee", "sip_to"):
        v = meta.get(key)
        if isinstance(v, str) and v.startswith("+"):
            return v
    # Strip optional prefix and look for E.164.
    import re as _re
    m = _re.search(r"\+\d{6,15}", room_name or "")
    return m.group(0) if m else None


def _lookup_assistant_for_number(phone_number: str) -> str | None:
    """Find the assistant assigned to this PSTN number. Returns None if the
    number doesn't exist, has no assistant assigned, or is ambiguous across
    multiple tenants.

    Cross-tenant safety: the same Twilio number can land in multiple users'
    `phone_numbers` docs (same Twilio credentials shared across team accounts).
    Returning the first match would route a caller to the wrong tenant's
    assistant. Instead we require an UNAMBIGUOUS resolution — exactly one
    assignment across all docs — and refuse to guess otherwise.
    """
    try:
        from app.config.database import Database
        db = Database.get_db()
        # Pull every doc for this phone number across all tenants.
        docs = list(db["phone_numbers"].find({"phone_number": phone_number}))
        # Keep only the docs that actually have an assigned assistant.
        with_assignment = [d for d in docs if d.get("assigned_assistant_id")]
        if not with_assignment:
            if docs:
                logger.warning(
                    "[AGENT] Number %s exists in %d tenant(s) but none have an "
                    "assistant assigned — cannot route inbound fallback",
                    phone_number, len(docs),
                )
            return None
        # Multiple tenants assigned conflicting assistants — refuse to guess.
        # Twilio's voice_url is the source of truth, so the agent should never
        # have hit this fallback in normal flow; if it did, abort safely.
        unique_ids = {str(d["assigned_assistant_id"]) for d in with_assignment}
        if len(unique_ids) > 1:
            logger.error(
                "[AGENT] Number %s has %d conflicting assignments across "
                "tenants: %s — refusing to route. Resolve via single-owner "
                "dedupe.",
                phone_number, len(unique_ids), unique_ids,
            )
            return None
        return next(iter(unique_ids))
    except Exception:
        logger.exception("[AGENT] Failed to look up assistant for %s", phone_number)
    return None


class ConvisAgent(Agent):
    def __init__(self, config: Dict[str, Any], ctx: JobContext) -> None:
        # The system_message is already FULLY assembled (calendar + expressive
        # + multilingual + RAG + end-call suffixes baked in) by
        # assistant_config.build_system_message(). We deliberately do NOT
        # mutate it here — any mutation would diverge from the cache warmer's
        # prompt and break OpenAI prompt-cache hits, regressing first-turn LLM
        # TTFT from ~0.5s to ~3-4s.
        #
        # Conversation memory: when the assistant has
        # `conversation_history_enabled=True` AND the entrypoint built a
        # contact_history_block, we pass it as a SECOND system message via
        # chat_ctx. Crucially this preserves the cache: OpenAI caches the
        # prefix-match of `messages`, so as long as messages[0] (the
        # instructions-derived system message) is byte-identical across all
        # calls of this assistant, the cache hits — the second per-call
        # message just contributes fresh tokens for that call. Verified
        # empirically: livekit.agents.Agent stores `instructions` and
        # `chat_ctx` separately and composes them at LLM-call time.
        history_block = config.get("contact_history_block")
        if history_block:
            initial_ctx = ChatContext.empty()
            initial_ctx.add_message(role="system", content=history_block)
            super().__init__(
                instructions=config["system_message"],
                chat_ctx=initial_ctx,
            )
        else:
            super().__init__(instructions=config["system_message"])
        self._config = config
        # Stash ctx so end_call() can identify the room to disconnect.
        self._ctx = ctx
        # Idle-silence watchdog state. Updated by mark_activity() whenever
        # an EOU or TTS metric fires (i.e. someone just spoke). The watchdog
        # task is started by the entrypoint after session.start().
        self._last_activity_ts = time.monotonic()
        self._hangup_in_progress = False
        # Holds the current idle_watchdog task. entrypoint sets this after
        # session.start(); the transfer-failure path re-creates it. _cancel_watchdog
        # (registered as a shutdown callback in entrypoint) cancels whatever it
        # currently points to.
        self._watchdog_task: Any = None
        # Holds the in-flight _speak_then_transfer task (if any) so the
        # shutdown callback can cancel it instead of leaving an orphaned task
        # ("Task exception was never retrieved" on a torn-down loop).
        self._transfer_task: Any = None
        self._idle_timeout = float(
            config.get("idle_timeout_seconds") or DEFAULT_IDLE_TIMEOUT_SECONDS
        )
        # Current AgentSession state, mirrored from the session's
        # agent_state_changed event (see entrypoint hookup).
        # Possible values per livekit-agents 1.5.x:
        #   "initializing" — session warming up
        #   "idle"         — between turns, agent waiting
        #   "listening"    — user is speaking
        #   "thinking"     — LLM streaming a response (NO TTS yet — first
        #                    sentence still being assembled by the tokenizer)
        #   "speaking"     — bot's TTS is playing audio
        # The idle_watchdog skips its timeout check while state is "thinking"
        # or "speaking" so a slow LLM (sarvam-105b can take 15-25s on
        # open-ended prompts) doesn't get its in-flight response cancelled
        # by the watchdog firing prematurely. The 45s timeout is now just a
        # safety net for genuinely hung calls.
        self._agent_state: str = "initializing"

    def _session_or_none(self) -> Any:
        """Return the active AgentSession, or None if it's been torn down.

        `Agent.session` is a property that raises RuntimeError when the
        activity context is gone (e.g. caller hung up and livekit started
        cleanup). `getattr(self, "session", None)` does NOT catch
        RuntimeError — only AttributeError — so we wrap it here. Also
        falls back to the private `_session` attr if available (older
        livekit-agents versions stored it there).
        """
        try:
            return self.session
        except Exception:
            pass
        return getattr(self, "_session", None)

    def _session_alive(self) -> bool:
        """Cheap check: is the session reference still valid? Used by
        idle_watchdog to avoid firing hangup on a session that's already
        being torn down (race with caller-hangup → SIP BYE → activity
        context disposed before our watchdog can fire)."""
        return self._session_or_none() is not None

    def mark_activity(self, audio_duration: float = 0.0) -> None:
        """Reset the idle timer. Called from the session metrics hook on every
        EOUMetrics (user finished speaking) and TTSMetrics (bot starting audio).

        TTSMetrics fires when audio GENERATION completes — but the audio then
        plays back to PSTN over the next `audio_duration` seconds. If we
        treated TTS as "instant activity", the watchdog would fire mid-speech
        on long bot responses (e.g. a 20-s answer would trigger hangup at the
        10-s default timeout, cutting the bot off). Instead, project the
        timestamp forward by the audio duration so the conversation isn't
        considered idle until the bot actually finishes speaking."""
        new_ts = time.monotonic() + max(0.0, audio_duration)
        # max() so a long-running TTS doesn't get clobbered by a quick EOU.
        if new_ts > self._last_activity_ts:
            self._last_activity_ts = new_ts

    async def idle_watchdog(self) -> None:
        """Background task: hang up the call after IDLE_TIMEOUT_SECONDS of
        no EOU/TTS events. Started from the entrypoint after session.start()."""
        room_name = getattr(self._ctx.room, "name", "<unknown>")
        logger.info(
            "[AGENT] idle_watchdog: armed (timeout=%.0fs) for room %s",
            self._idle_timeout, room_name,
        )
        while True:
            await asyncio.sleep(_IDLE_WATCHDOG_POLL_SECONDS)
            if self._hangup_in_progress:
                return  # end_call already firing — stand down
            # Race-protection: if the session has already been disposed
            # (caller hung up, livekit started cleanup, but our shutdown
            # callback hasn't yet cancelled this task), exit silently
            # instead of trying to fire farewell on a dead session.
            if not self._session_alive():
                logger.info(
                    "[AGENT] idle_watchdog: session already gone for %s, exiting cleanly",
                    room_name,
                )
                return
            # LLM-aware suppression. While the agent is "thinking" (LLM
            # streaming tokens — TTS hasn't started yet because the
            # tokenizer is still assembling the first sentence) or
            # "speaking" (TTS playing audio), keep refreshing the activity
            # timestamp instead of counting toward timeout. This prevents
            # the watchdog from cancelling a slow but progressing LLM
            # response (observed on sarvam-105b with "explain X" prompts
            # where the first sentence took 15-20s to complete). The 45s
            # timeout still applies if the agent state itself stays stuck
            # for that long — a real hang, not just slow streaming.
            if self._agent_state in ("thinking", "speaking"):
                self._last_activity_ts = time.monotonic()
                continue
            elapsed = time.monotonic() - self._last_activity_ts
            if elapsed >= self._idle_timeout:
                logger.info(
                    "[AGENT] idle_watchdog: %.1fs of silence on room %s — auto hangup",
                    elapsed, room_name,
                )
                self._hangup_in_progress = True
                # Speak a soft sign-off so caller hears something before the
                # SIP drop (otherwise the line just goes dead which feels rude).
                await self._farewell_and_hangup(
                    "I'll let you go for now. Have a great day!"
                )
                return

    @function_tool
    async def search_knowledge_base(self, query: str) -> str:
        """Search the official documentation for information relevant to the
        caller's question. Call this DIRECTLY for any factual lookup — do NOT
        preface with 'let me check' or 'one moment' (the system handles the
        brief gap). After results return, answer in 1-2 sentences.

        Args:
            query: The caller's question or topic to look up. Use the user's
                own phrasing — don't paraphrase aggressively.

        Returns:
            Relevant excerpts from the documentation, or a short "no result"
            sentinel string if nothing matches. NEVER returns an empty string
            — see the rationale below.
        """
        # MUST NEVER RETURN AN EMPTY STRING.
        # Sarvam-105b's chat-completions endpoint rejects messages where a
        # tool's response content is an empty string with HTTP 400:
        #   "body.messages.N.tool.content : String should have at least 1 character"
        # When that happens LiveKit retries the request 4 times, sees the
        # same 400 every time, raises APIConnectionError, and silently drops
        # the turn — the user hears nothing, the watchdog eventually fires
        # the farewell. Returning a short prose sentinel keeps Sarvam happy
        # and lets the LLM apologise to the caller naturally.
        _NO_RESULT = (
            "No relevant information was found in the knowledge base for "
            "this query. Apologise briefly to the caller and suggest a "
            "human follow-up if appropriate."
        )
        _NO_KB = (
            "This assistant has no knowledge base configured. Apologise "
            "briefly and answer from general knowledge if you can."
        )
        try:
            from app.utils.mongo_rag import search, build_context_for_voice
            assistant_id = self._config.get("assistant_id")
            if not assistant_id:
                return _NO_KB
            # Run sync Mongo + sync OpenAI embedding off the event loop so the
            # audio pipeline (STT/TTS streaming) doesn't stall while we search.
            results = await asyncio.to_thread(
                search, assistant_id, query, 5, None,
            )
            content = build_context_for_voice(results) or ""
            if not content.strip():
                return _NO_RESULT
            return content
        except Exception as e:
            logger.warning("[RAG] search_knowledge_base failed: %s", e)
            return _NO_RESULT

    @function_tool
    async def end_call(self, farewell: str) -> str:
        """End the call gracefully. The bot will speak the provided farewell
        aloud, wait for the audio to finish, then disconnect the call.

        Call this only when the caller has clearly finished the conversation
        — they said goodbye, "that's all I needed", "thanks bye", "have a
        good day", etc. Do NOT call on simple acknowledgments like "ok" or
        "thanks" alone (they often continue speaking).

        Args:
            farewell: A brief one-sentence farewell to speak before
                disconnecting (e.g. "Thanks for calling, take care!").
        """
        # Dedup: with parallel_tool_calls enabled (OpenAI default), the LLM
        # can emit end_call twice in a single response. Without this guard
        # we'd spawn two _farewell_and_hangup tasks → two session.say()
        # collisions + two delete_room API calls.
        if self._hangup_in_progress:
            return "already_ending"
        # Spawn the speak+disconnect in the background so this tool returns
        # immediately. Returning "ending" lets livekit close the tool round-
        # trip cleanly; the actual goodbye + room-delete happens off-band.
        # Also flag in_progress so the idle watchdog stands down.
        self._hangup_in_progress = True
        asyncio.create_task(self._farewell_and_hangup(farewell))
        return "ending"

    async def _farewell_and_hangup(self, farewell: str) -> None:
        """Speak the farewell deterministically (no LLM paraphrase), wait for
        TTS to finish playing, then delete the LiveKit room.

        Resilient by design: if the session is already disposed (caller hung
        up just before this fired — common race when watchdog fires), skip
        the farewell speak silently and proceed to room delete. delete_room
        is idempotent — if the room is already gone, the API returns success.
        """
        room_name = getattr(self._ctx.room, "name", "<unknown>")
        session = self._session_or_none()
        if session is None:
            # Caller hung up first / session torn down — skip farewell.
            # No need to log a warning; this is the expected race outcome.
            logger.info(
                "[AGENT] end_call: session already disposed for %s, skipping farewell",
                room_name,
            )
        else:
            try:
                speech_handle = session.say(farewell, allow_interruptions=False)
                # session.say() returns a SpeechHandle we can await for
                # completion. Older versions return None — fall back to a
                # word-count estimate.
                try:
                    await speech_handle  # type: ignore[func-returns-value]
                except TypeError:
                    est = max(1.0, min(5.0, len(farewell.split()) * 0.35))
                    await asyncio.sleep(est)
                # Tiny buffer so the last audio frame flushes through PSTN.
                await asyncio.sleep(0.4)
            except Exception:
                logger.warning(
                    "[AGENT] end_call: farewell playback failed for %s",
                    room_name, exc_info=True,
                )

        # Always attempt room delete — defensive cleanup. delete_room is
        # idempotent. If the room is already gone via natural disconnect,
        # the API call either returns success or NotFound (caught below).
        try:
            from livekit import api
            from app.services.livekit.tokens import livekit_api_client
            lk = livekit_api_client()
            try:
                await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
                logger.info(
                    "[AGENT] end_call: room %s deleted (graceful hangup)",
                    room_name,
                )
            finally:
                await lk.aclose()
        except Exception as e:
            # Most common: room already deleted (NotFound) — that's fine,
            # cleanup happened via livekit's natural disconnect path. Log at
            # info, not warning, to avoid alarming people.
            logger.info(
                "[AGENT] end_call: room %s delete skipped (%s)",
                room_name, type(e).__name__,
            )

    # ── Call transfer to a human agent ──────────────────────────────────────
    # Always defined (like search_knowledge_base) — the codebase never
    # conditionally registers tools; the PROMPT is what gates whether the LLM
    # knows to use it (the call-transfer suffix is only appended to the system
    # message when transfer is effectively enabled, see assistant_config.py).
    # The body re-checks the gates defensively (a Vobiz/LiveKit-direct call
    # reaches here with no Twilio call_sid → "transfer not available").
    @function_tool
    async def transfer_to_agent(self, reason: str) -> str:
        """Connect the caller to a human team member. Call this when you cannot
        fulfil their request per your role, the caller is clearly frustrated
        after you've tried, or they explicitly ask for a person.

        Pass a brief `reason` (e.g. "caller wants a refund I can't process").
        The system will tell the caller it's connecting them and complete the
        handoff — do NOT say anything else after calling this.

        Args:
            reason: One short phrase: why you're transferring. Logged for the
                operator; not spoken to the caller.
        """
        # Dedup / mutual-exclusion with end_call and the idle watchdog. If a
        # hangup OR a prior transfer is already underway, no-op.
        if self._hangup_in_progress:
            return "already_ending"

        # Hard server-side guard: a call that's resuming after a previously
        # failed transfer never gets a second attempt (see _resolve_config).
        # This also covers the case where the prompt suffix is still present.
        if self._config.get("resumed_after_failed_transfer"):
            logger.info("[AGENT] transfer_to_agent declined: resumed-after-failed-transfer call")
            return "transfer not available"

        call_sid = self._config.get("call_sid") or await self._lookup_twilio_call_sid()
        target = (self._config.get("call_transfer_number") or "").strip()
        if not (self._config.get("call_transfer_enabled") and target and call_sid):
            # Not configured for this assistant, or this call has no Twilio
            # CallSid we can redirect (Vobiz / LiveKit-direct SIP).
            logger.info(
                "[AGENT] transfer_to_agent declined: enabled=%s target=%r call_sid=%r",
                self._config.get("call_transfer_enabled"), target, call_sid,
            )
            return "transfer not available"

        self._hangup_in_progress = True  # stand down the idle watchdog & dedup end_call
        hold_message = (self._config.get("call_transfer_message") or "").strip() \
            or "Let me connect you with a member of our team — please hold."
        self._transfer_task = asyncio.create_task(self._speak_then_transfer(
            target=target,
            call_sid=call_sid,
            hold_message=hold_message,
            reason=(reason or "").strip()[:300],
        ))
        return "transferring"

    async def _lookup_twilio_call_sid(self) -> Optional[str]:
        """Best-effort: find the Twilio CallSid for this room from call_logs.
        Used for Twilio-twiml outbound calls where the CallSid wasn't known at
        room-creation time so it isn't in room metadata. Returns None for
        Vobiz/LiveKit-direct calls (no Twilio leg). Sync Mongo is run off the
        event loop so the audio pipeline doesn't stall."""
        try:
            def _query():
                from app.config.database import Database
                return Database.get_db()["call_logs"].find_one(
                    {"livekit_room": self._ctx.room.name},
                    {"twilio_call_sid": 1, "call_sid": 1},
                )
            doc = await asyncio.to_thread(_query)
            if not doc:
                return None
            tcs = doc.get("twilio_call_sid")
            if tcs:
                return str(tcs)
            cs = doc.get("call_sid")
            # For inbound calls `call_sid` IS the Twilio CallSid (CA...). For
            # Vobiz/LiveKit-direct it's the room name — not redirectable.
            if isinstance(cs, str) and cs.startswith("CA"):
                return cs
        except Exception:
            logger.debug("[AGENT] _lookup_twilio_call_sid failed", exc_info=True)
        return None

    async def _speak_then_transfer(
        self, *, target: str, call_sid: str, hold_message: str, reason: str
    ) -> None:
        """Speak the hold message, stamp the call_log, then redirect the live
        Twilio call to `target`. On failure: reset state, re-arm the idle
        watchdog, apologise, and let the conversation continue. Mirrors the
        resilience pattern of _farewell_and_hangup."""
        from datetime import datetime, timezone
        room_name = getattr(self._ctx.room, "name", "<unknown>")
        session = self._session_or_none()
        if session is not None:
            try:
                speech_handle = session.say(hold_message, allow_interruptions=False)
                try:
                    await speech_handle  # type: ignore[func-returns-value]
                except TypeError:
                    est = max(1.0, min(5.0, len(hold_message.split()) * 0.35))
                    await asyncio.sleep(est)
                await asyncio.sleep(0.4)  # PSTN flush
            except Exception:
                logger.warning("[AGENT] transfer: hold-message playback failed for %s", room_name, exc_info=True)

        async def _stamp(fields: Dict[str, Any]) -> None:
            # Sync pymongo off the event loop — never block the audio pipeline.
            def _do():
                from app.config.database import Database
                Database.get_db()["call_logs"].update_many(
                    {"call_sid": call_sid}, {"$set": {**fields, "updated_at": datetime.now(timezone.utc)}}
                )
            try:
                await asyncio.to_thread(_do)
            except Exception:
                logger.debug("[AGENT] transfer: call_log stamp failed", exc_info=True)

        now = datetime.now(timezone.utc)
        await _stamp({
            "transferred": True, "transferred_to": target, "transfer_outcome": "dialing",
            "transfer_reason": reason, "transferred_at": now,
        })

        from app.services.livekit.sip_service import transfer_twilio_call_to_number
        ok = await transfer_twilio_call_to_number(
            call_sid=call_sid, target_number=target,
            owner_user_id=self._config.get("user_id"),
            direction=self._config.get("direction") or "inbound",
        )

        if ok:
            # Belt-and-suspenders: Twilio's calls.update() already drops the
            # SIP leg into LiveKit (room empties → job ends → _mark_call_completed
            # fires), but delete the room explicitly too in case of races.
            await asyncio.sleep(2.0)
            try:
                from livekit import api as _lk_api
                from app.services.livekit.tokens import livekit_api_client
                lk = livekit_api_client()
                try:
                    await lk.room.delete_room(_lk_api.DeleteRoomRequest(room=room_name))
                finally:
                    await lk.aclose()
            except Exception:
                logger.info("[AGENT] transfer: room %s delete skipped", room_name)
            return

        # Transfer failed — recover. Re-arm the idle watchdog (the original one
        # returned when _hangup_in_progress went True) and keep the call going.
        logger.warning("[AGENT] transfer to %s failed for room %s — recovering", target, room_name)
        await _stamp({"transferred": False, "transfer_failed": True, "transfer_outcome": "redirect-rejected"})
        self._hangup_in_progress = False
        try:
            self._watchdog_task = asyncio.create_task(self.idle_watchdog())
        except Exception:
            logger.debug("[AGENT] transfer: failed to re-arm idle watchdog", exc_info=True)
        session = self._session_or_none()
        if session is not None:
            try:
                session.say(
                    "I'm sorry, I wasn't able to connect you right now — let me keep helping.",
                    allow_interruptions=True,
                )
            except Exception:
                logger.debug("[AGENT] transfer: recovery message failed", exc_info=True)

    # ── Outbound follow-up workflow tools ─────────────────────────────────
    # Always defined on the class (mirrors search_knowledge_base /
    # transfer_to_agent). The PROMPT-SIDE suffix is what gates whether the
    # LLM is taught to call them; these bodies also re-check the gate so a
    # stray model call on a non-follow-up assistant returns "not available"
    # instead of silently writing to Mongo / Google Calendar.
    @function_tool
    async def record_filing_status(self, filed: bool, notes: str) -> str:
        """Record whether the client confirmed completion of the follow-up
        topic (e.g. "have you filed your taxes?"). MUST be called once per
        call — before `book_followup_appointment`, and even if the answer
        is yes (so the outcome is captured if the call drops).

        Args:
            filed: True if the client confirmed they completed it; False
                if they have not yet done it (or are unsure / refused to say).
            notes: A one-sentence summary of what they said, in their own
                words. Keep under 300 characters; logged for the operator.
        """
        from app.services.livekit.outbound_followup_service import (
            is_followup_effective,
            record_filing_status as _record,
        )
        if not is_followup_effective(self._config):
            return "not available"
        try:
            res = await _record(
                filed=bool(filed),
                notes=notes,
                livekit_room=getattr(self._ctx.room, "name", None),
                call_sid=self._config.get("call_sid"),
                assistant_id=self._config.get("assistant_id"),
            )
            return "recorded" if res.get("ok") else "recorded_orphan"
        except Exception:
            logger.exception("[AGENT] record_filing_status failed")
            return "error"

    @function_tool
    async def book_followup_appointment(
        self,
        start_iso: str,
        duration_minutes: int,
        client_name: str,
    ) -> str:
        """Book a real calendar appointment with the configured CA and fire
        WhatsApp confirmations to both the client and the CA. Only call this
        AFTER `record_filing_status` returned with filed=false AND the client
        explicitly agreed to a specific time.

        Args:
            start_iso: ISO-8601 start time WITH timezone offset, e.g.
                "2026-05-22T11:00:00+05:30". Must be a slot the client
                explicitly confirmed — never invent times.
            duration_minutes: Length of the appointment in minutes. Use the
                default (typically 30) unless the client asks for longer.
            client_name: The client's name exactly as they gave it on the
                call. Used in the WhatsApp confirmation.

        Returns:
            A short status: "confirmed" (event + WA all good),
            "confirmed_no_wa" (event booked but WhatsApp failed — tell the
            client they're booked and a team member will follow up), or
            "failed" (could not book — apologise and offer a callback).
        """
        from app.services.livekit.outbound_followup_service import (
            is_followup_effective,
            book_appointment_and_notify,
        )
        if not is_followup_effective(self._config):
            return "not available"

        user_id = self._config.get("user_id")
        assistant_id = self._config.get("assistant_id")
        # Client phone arrives via room metadata for outbound campaign calls.
        # If it's not set (rare — manual outbound or test bench), the WA send
        # to the client silently no-ops and we return "confirmed_no_wa".
        client_phone = (
            self._config.get("customer_phone")
            or self._config.get("to_number")
            or ""
        )
        if not user_id or not assistant_id:
            return "not available"

        try:
            res = await book_appointment_and_notify(
                user_id=str(user_id),
                assistant_id=str(assistant_id),
                livekit_room=getattr(self._ctx.room, "name", None),
                call_sid=self._config.get("call_sid"),
                client_phone=client_phone,
                client_name=(client_name or "").strip()[:80] or "Customer",
                start_iso=start_iso,
                duration_minutes=int(duration_minutes or self._config.get("appointment_duration_minutes") or 30),
                timezone_str=self._config.get("appointment_timezone") or "UTC",
                ca_name=self._config.get("ca_name") or "our specialist",
                ca_phone=self._config.get("ca_phone") or "",
                ca_calendar_account_id=self._config.get("ca_calendar_account_id") or "",
                firm_name=self._config.get("firm_name") or "",
                wa_template_client=self._config.get("wa_template_client") or "",
                wa_template_ca=self._config.get("wa_template_ca") or "",
                topic=self._config.get("outbound_followup_topic") or "your appointment",
            )
        except Exception:
            logger.exception("[AGENT] book_followup_appointment raised")
            return "failed"

        if not res.get("ok"):
            return "failed"
        # Treat "client WhatsApp didn't go through" as the degraded-but-OK
        # path. The CA-side WA is internal; if only it fails, the booking is
        # still effectively confirmed from the client's perspective.
        if not res.get("wa_client"):
            return "confirmed_no_wa"
        return "confirmed"


# ── Latency knobs ────────────────────────────────────────────────────────────
# Tuned for ~700 ms warm time-to-first-audio on PSTN — the "snappy" profile.
# These defaults are conservative enough not to cut callers off mid-sentence
# (the 130/0.08 preset we shipped previously fired on filler "uh"/"um" and
# caused agents to talk over slow speakers). Override per-assistant via room
# metadata (see _resolve_config) — the "patient" profile bumps these to
# 300/0.20/0.60 for elderly-care / healthcare workflows.
DEFAULT_MIN_ENDPOINTING_DELAY = 0.15  # AgentSession turn-end debounce (seconds)
DEFAULT_MIN_INTERRUPTION_DURATION = 0.25  # min speech to trigger barge-in (seconds)
# Sarvam Saaras v3 — Indic-language ASR, supports all 22 Indian languages plus
# en-IN. Used with mode="transcribe" so the transcript stays in the caller's
# original language (the alternative `translate` mode forces output to English,
# which would break Hindi/regional voice agents because the LLM and TTS need
# to see the actual spoken language).
DEFAULT_ASR_MODEL = "saaras:v3"
DEFAULT_ASR_MODE = "transcribe"
DEFAULT_ASR_LANGUAGE = "en-IN"
# Hard cap on LLM output length. The system prompt asks for "1-2 sentences"
# but gpt-4o-mini sometimes legitimately needs more (list-style questions,
# document-grounded answers). 250 tokens (~190 words ≈ 70 s of speech) gives
# headroom so chatty turns don't get cut off mid-sentence. Time-to-first-audio
# is unchanged (TTS streams from token #1); only total speech length grows
# when the LLM actually needs the room.
DEFAULT_LLM_MAX_TOKENS = 250

# Idle silence timeout. If neither user nor bot speaks for this many seconds
# (after the last completed turn), the agent gracefully hangs up. Prevents
# rooms hanging open after the caller leaves without saying goodbye.
#
# 45s post-Sarvam migration (was 15s on the gpt-4o-mini + warm prompt cache
# stack). Sarvam-105b has NO prompt cache and processes the full 4K-token
# system prompt every turn — observed TTFT ranges from 1-3s on short factual
# questions to 15-25s on open-ended "explain X" questions where the model
# generates a long answer. With the old 15s ceiling the watchdog kept firing
# mid-LLM-call, cancelling the in-flight response and speaking the farewell
# ("I'll let you go for now. Have a great day!") to a caller who had just
# asked a question. 45s gives sarvam-105b headroom while still hanging up
# on genuinely abandoned calls. Tune downward later if we add a per-turn
# "LLM in flight" gate to mark_activity that suppresses the watchdog during
# active LLM processing.
DEFAULT_IDLE_TIMEOUT_SECONDS = 45.0
# How often the watchdog polls for silence. Cheap async sleep, no I/O.
_IDLE_WATCHDOG_POLL_SECONDS = 1.0


def prewarm(proc: "agents.JobProcess") -> None:
    """Load expensive models AND warm provider TLS/DNS BEFORE any job arrives.

    LiveKit calls this once per worker process at startup. Two things happen:

    1. **Silero VAD** is loaded into proc.userdata and reused across every job
       this worker takes. Without this, the first job pays Silero VAD load
       (~500 ms) and ONNX init.

    2. **Provider DNS + TLS session cache warmup**: one-shot HEAD request to
       each provider host. We can't share the SDK's httpx connection pool
       (each plugin carries its own client internally), but TLS 1.3 session
       tickets persist at the OS level for ~24h — so when the SDK opens its
       real connection on the first user turn, it pays a TLS resumption (~50ms)
       instead of a full handshake (~250ms). Saves ~150-200ms on the FIRST
       audio frame from the user (the cold path that previously caused the
       "lag right after greeting" complaint).

    Failure to prewarm should NOT crash the worker — every job has a runtime
    fallback (_get_vad) that loads on demand. We log loudly so operators know
    they're paying the per-job cost until the prewarm cause is fixed.
    """
    try:
        proc.userdata["vad"] = silero.VAD.load()
        logger.info("[AGENT] prewarm: Silero VAD loaded")
    except Exception:
        logger.exception(
            "[AGENT] prewarm failed to load Silero VAD; falling back to "
            "per-job VAD load (each call pays ~500 ms init)"
        )
        proc.userdata["vad"] = None

    # Pre-warm the MongoDB connection. Without this, the FIRST DB access in a
    # fresh worker process (typically the shutdown callback's call_log lookup)
    # races the lazy Database.connect() inside Database.get_db() — observed
    # symptom: `TypeError: 'NoneType' object is not subscriptable` followed by
    # a delayed "Successfully connected to MongoDB" log because the connect
    # had to chase down DNS + initial server handshake while the callback
    # was already running. Doing it here at prewarm gives us a hot pool
    # before any room is dispatched.
    try:
        from app.config.database import Database
        Database.connect()
        logger.info("[AGENT] prewarm: MongoDB connection established")
    except Exception:
        logger.exception(
            "[AGENT] prewarm failed to establish MongoDB connection; "
            "shutdown callbacks may log NoneType errors on first call"
        )

    _warm_provider_tls()


def _warm_provider_tls() -> None:
    """Make a one-shot HTTPS request to each provider to populate the OS-level
    DNS cache and TLS 1.3 session ticket store. Errors are swallowed — this is
    best-effort and we don't care about the response (HEAD typically returns
    401/404 — we just want the handshake done).
    """
    import socket
    import urllib.request
    hosts = ("api.sarvam.ai",)
    warmed = []
    for host in hosts:
        try:
            socket.gethostbyname(host)
        except Exception:
            continue
        try:
            req = urllib.request.Request(f"https://{host}/", method="HEAD")
            urllib.request.urlopen(req, timeout=2.0)
            warmed.append(host)
        except Exception:
            # 401/403/404 are expected and still complete the TLS handshake,
            # which is the whole point. Network errors / timeouts we ignore.
            warmed.append(host)
    logger.info("[AGENT] prewarm: TLS+DNS warmed for %s", warmed)


def _get_vad(ctx: JobContext):
    """Return the prewarmed VAD if available, else load on-the-fly.

    On-the-fly is the test-mode fallback (conftest's stub JobContext has no
    .proc) and a safety net if prewarm_fnc didn't run for any reason.
    """
    proc_data = getattr(getattr(ctx, "proc", None), "userdata", None)
    if proc_data and "vad" in proc_data:
        return proc_data["vad"]
    return silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    logger.info("[AGENT] Job received for room %s", ctx.room.name)

    config = _resolve_config(ctx)
    logger.info(
        "[AGENT] Resolved config for assistant_id=%s name=%s",
        config.get("assistant_id"),
        config.get("name"),
    )
    # Explicit ASR / multilingual / KB log line so production calls leave a
    # trail we can grep when diagnosing language-switching bugs.
    logger.info(
        "[AGENT] ASR=Sarvam model=%s mode=%s lang=%s | multilingual=%s expressive=%s kb=%s | system_msg_chars=%d",
        config.get("asr_model"), config.get("asr_mode"), config.get("asr_language"),
        config.get("multilingual"), config.get("expressive_mode"),
        config.get("has_knowledge_base"),
        len(config.get("system_message") or ""),
    )
    logger.info(
        "[AGENT] call_transfer=%s number=%s call_sid=%s resumed=%s",
        config.get("call_transfer_enabled"), config.get("call_transfer_number"),
        config.get("call_sid"), config.get("resumed_after_failed_transfer"),
    )

    # Conversation memory across calls. When the assistant has
    # `conversation_history_enabled=True`, look up the contact for this
    # phone number and inject the last N call summaries as a SEPARATE
    # second system message. The primary system_message stays byte-
    # identical to what the cache warmer produces, so OpenAI prompt-cache
    # hits on the base prompt are preserved (the new fresh tokens come
    # AFTER the cached prefix). See conversation_history_service.py and
    # the chat_ctx branch in ConvisAgent.__init__ below.
    config["contact_history_block"] = None
    if config.get("conversation_history_enabled"):
        direction = (config.get("direction") or "").lower()
        # Use the SHARED resolver so this pre-call lookup and the post-call
        # write (post_call_summary_service.extract_and_persist_summary)
        # identify the contact by the EXACT same phone number — otherwise the
        # summary is saved under one contact and read from another, and
        # conversation memory silently no-ops.
        from app.services.contact_service import resolve_contact_phone
        contact_phone = resolve_contact_phone(
            direction=config.get("direction"),
            from_number=config.get("from_number"),
            to_number=config.get("to_number"),
            customer_phone=config.get("customer_phone"),
        )
        if contact_phone and config.get("user_id"):
            try:
                from app.services.conversation_history_service import build_context_block
                block = await build_context_block(
                    user_id=config["user_id"],
                    phone_number=contact_phone,
                    max_calls=int(config.get("conversation_history_max_calls") or 3),
                )
                config["contact_history_block"] = block
                logger.info(
                    "[AGENT] conversation_history: phone=%s block_chars=%d",
                    contact_phone, len(block or ""),
                )
            except Exception:
                logger.exception(
                    "[AGENT] conversation_history build failed for phone=%s "
                    "— falling back to no context (call proceeds normally)",
                    contact_phone,
                )
        else:
            logger.info(
                "[AGENT] conversation_history enabled but no resolvable phone/user "
                "(direction=%s); skipping", direction or "?",
            )

    # Sarvam Saaras v3 STT — streaming Indic-language ASR over WSS, with
    # interim results and server-side VAD (the plugin's STTCapabilities
    # advertises streaming=True + interim_results=True). Saaras v3 supports
    # all 22 Indic languages plus en-IN and code-switching mid-utterance.
    #
    # mode="transcribe" — keeps the transcript in the caller's source language.
    # The alternative `translate` mode would force English output, which would
    # break Hindi/regional conversations (the LLM and TTS need to see the
    # actual spoken language to reply in kind).
    #
    # language="unknown" for multilingual mode → Sarvam auto-detects per
    # utterance across all 22 languages. Otherwise pin to the configured
    # language code (e.g. "hi-IN", "en-IN") for tighter accuracy.
    #
    # Knobs that no longer apply (vs the old Deepgram path):
    #   - endpointing_ms: Sarvam uses server-side VAD via positive/negative
    #     speech-threshold params + frame counts, not a single ms knob.
    #     Leaving plugin defaults; can tune later per-assistant if needed.
    #   - keywords / keyterm: Sarvam does NOT support keyword biasing.
    #     The field `asr_keywords` in Mongo is now inert.
    #   - smart_format / no_delay: Sarvam handles these internally.
    #
    # high_vad_sensitivity=True biases toward catching short user replies
    # ("haan", "okay") which matters for natural turn-taking on PSTN.
    asr_model = config.get("asr_model") or DEFAULT_ASR_MODEL
    asr_mode = config.get("asr_mode") or DEFAULT_ASR_MODE
    asr_language = config.get("asr_language") or DEFAULT_ASR_LANGUAGE
    stt = sarvam.STT(
        model=asr_model,
        mode=asr_mode,
        language=asr_language,
        high_vad_sensitivity=True,
    )

    # Sarvam LLM — token streaming (sarvam.LLM extends livekit-plugins-openai
    # LLM with OpenAI-compatible chat completions against api.sarvam.ai/v1).
    #
    # max_tokens hard-caps response length so a chatty turn can't blow past
    # ~30s of speech. The plugin forwards `max_tokens` via extra_body (Sarvam
    # uses the legacy name, not OpenAI's newer max_completion_tokens).
    #
    # sarvam-105b runs in "thinking" mode by default — it emits
    # <think>...</think> reasoning blocks before the actual answer, adding
    # 2-5s of latency per turn. assistant_config.build_system_message()
    # prepends "/nothink" at the very start of every system prompt to disable
    # this. Without /nothink the agent is unusable for live voice calls.
    #
    # NOTE on prompt cache: Sarvam has no equivalent of OpenAI's
    # prompt_cache_key, so the llm_cache_warmer.py background loop is dead
    # code (removed). First-turn TTFT goes from ~500ms (cached gpt-4o-mini)
    # to ~1.5-3s on sarvam-105b. Tradeoff accepted by product (full Indian
    # stack, no Western fallback).
    llm = sarvam.LLM(
        model=config.get("llm_model") or "sarvam-105b",
        temperature=config.get("temperature", 0.7),
        max_tokens=int(config.get("llm_max_tokens") or DEFAULT_LLM_MAX_TOKENS),
    )

    # TTS — Sarvam Bulbul (the only supported provider). Streams audio chunks
    # over WSS as they synthesize.
    #
    # Default model bulbul:v3 with default speaker "shubh" (male Hindi). v3
    # is the streaming-capable flagship; livekit-plugins-sarvam 1.5.12
    # correctly sends temperature + omits pitch/loudness for v3.
    #
    # v3 has a different speaker catalogue than v2 — anushka/manisha/vidya/
    # arya/abhilash/karun/hitesh are v2-ONLY and will raise ValueError on
    # v3 init. assistant_config._coerce_tts_voice enforces per-model
    # compatibility (anushka+v3 → shubh, pooja+v2 → anushka, etc.) so a
    # stale Mongo doc can't crash the agent here.
    #
    # `tts_speed` from Mongo maps to Sarvam's `pace` (1.0 default).
    # enable_preprocessing is v2-only inside the plugin (silently ignored on
    # v3) so leaving it True is harmless for the default path.
    tts = sarvam.TTS(
        target_language_code=config.get("tts_language") or "en-IN",
        model=config.get("tts_model") or "bulbul:v3",
        speaker=config.get("tts_voice") or "shubh",
        speech_sample_rate=22050,
        pace=float(config.get("tts_speed", 1.0)),
        enable_preprocessing=True,
        output_audio_codec="mp3",
    )
    logger.info(
        "[AGENT] TTS=Sarvam model=%s speaker=%s lang=%s pace=%s",
        config.get("tts_model") or "bulbul:v3",
        config.get("tts_voice") or "shubh",
        config.get("tts_language") or "en-IN",
        config.get("tts_speed", 1.0),
    )

    # Silero VAD — prewarmed if possible.
    vad = _get_vad(ctx)

    session = AgentSession(
        vad=vad,
        stt=stt,
        llm=llm,
        tts=tts,
        # Snap from "user finished" → LLM as fast as is sane.
        min_endpointing_delay=float(
            config.get("min_endpointing_delay") or DEFAULT_MIN_ENDPOINTING_DELAY
        ),
        # Don't treat coughs / breath sounds as barge-in.
        min_interruption_duration=float(
            config.get("min_interruption_duration") or DEFAULT_MIN_INTERRUPTION_DURATION
        ),
        max_tool_steps=config.get("max_tool_calls_per_turn", 5),
    )

    # ── Per-turn timing logs ────────────────────────────────────────────────
    # LiveKit emits structured metrics events for every STT/LLM/TTS/EOU step.
    # Logging them lets us see exactly where lag comes from (ASR finalize, LLM
    # time-to-first-token, TTS time-to-first-audio) per turn, in CloudWatch.
    # Create the agent up-front so the metrics callback can reset its idle
    # watchdog timer on EOU/TTS events (couldn't be done if we instantiated
    # the agent inline at session.start time below).
    agent = ConvisAgent(config, ctx)

    # ── Transcript logging ─────────────────────────────────────────────────
    # Log every user transcript + bot reply to CloudWatch. Without this we
    # can't diagnose "bot didn't switch to Hindi" type complaints — we just
    # see EOUMetrics / TTSMetrics / LLMMetrics with no idea what was actually
    # said. This is debug-grade but cheap (just a log line per turn).
    @session.on("conversation_item_added")
    def _on_conversation_item(ev: Any) -> None:
        try:
            item = getattr(ev, "item", None)
            if item is None:
                return
            role = getattr(item, "role", None)
            # ChatMessage.text_content() in newer livekit-agents; fall back
            # to .content (could be list of parts).
            text = ""
            try:
                tc = getattr(item, "text_content", None)
                if callable(tc):
                    text = tc() or ""
                else:
                    content = getattr(item, "content", None)
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = " ".join(str(c) for c in content if c)
            except Exception:
                pass
            # Skip empty/whitespace-only transcripts — pure log noise.
            if not text or not text.strip():
                return
            # Detect script for quick language diagnosis (Devanagari = Hindi/
            # Marathi, Arabic script, CJK, Latin). Helps spot ASR mistranscribe.
            script = "latin"
            for ch in text[:200]:
                cp = ord(ch)
                if 0x0900 <= cp <= 0x097F:
                    script = "devanagari"; break
                if 0x0600 <= cp <= 0x06FF:
                    script = "arabic"; break
                if 0x4E00 <= cp <= 0x9FFF:
                    script = "cjk"; break
            # Normalize all whitespace control chars in the preview. \r in
            # particular causes CloudWatch console to overwrite previous log
            # content (carriage return), which can hide bugs.
            preview = (
                text[:240]
                .replace("\n", " ")
                .replace("\r", " ")
                .replace("\t", " ")
            )
            logger.info(
                "[TRANSCRIPT] role=%s script=%s len=%d text=%r",
                role, script, len(text), preview,
            )
        except Exception:
            logger.debug("[TRANSCRIPT] formatter failed", exc_info=True)

    @session.on("metrics_collected")
    def _on_metrics(ev: Any) -> None:
        try:
            m = ev.metrics
            kind = type(m).__name__
            # Reset idle watchdog. Three classes of "activity":
            #  (1) TTSMetrics — bot is speaking. Project forward by audio
            #      duration so a 20-s answer doesn't get hung up at 10-s.
            #  (2) EOUMetrics — user finished a turn (transcript finalized).
            #  (3) STTMetrics with a non-empty request_id AND audio > 0 —
            #      Deepgram is buffering REAL user audio mid-utterance (no
            #      EOU yet because user hasn't paused enough). Without this
            #      branch, callers who speak slowly get hung up while their
            #      audio is sitting in Deepgram's buffer. Empty request_id
            #      indicates a session-init or 5-s keepalive (audio_duration
            #      jumps to 5.05 every poll regardless of speech) — those
            #      MUST be filtered out or the watchdog never fires.
            if kind == "TTSMetrics":
                agent.mark_activity(getattr(m, "audio_duration", 0.0) or 0.0)
            elif kind == "EOUMetrics":
                agent.mark_activity()
            elif kind == "STTMetrics":
                rid = getattr(m, "request_id", "") or ""
                ad = getattr(m, "audio_duration", 0.0) or 0.0
                if rid and ad > 0.0:
                    agent.mark_activity()
            # Common useful fields across metric types — log only what's set.
            fields: Dict[str, Any] = {"kind": kind}
            for attr in ("ttfb", "duration", "audio_duration", "request_id",
                         "tokens_per_second", "completion_tokens", "prompt_tokens",
                         "total_tokens", "end_of_utterance_delay",
                         "transcription_delay",
                         # OpenAI prompt caching — tells us how many of the
                         # prompt_tokens came from cache (50% cheaper, faster
                         # TTFT). With a 3,400-token system prompt we expect
                         # ~3,000 cached after the first turn within 5–10 min.
                         "cached_tokens", "prompt_cached_tokens"):
                v = getattr(m, attr, None)
                if v is not None:
                    fields[attr] = round(v, 4) if isinstance(v, float) else v
            # Also peek at nested .usage.prompt_tokens_details (OpenAI shape).
            details = getattr(m, "prompt_tokens_details", None)
            if details:
                cached = getattr(details, "cached_tokens", None)
                if cached is not None:
                    fields["cached_tokens"] = cached
            logger.info("[METRICS] %s", fields)
        except Exception:
            logger.debug("[METRICS] unable to format event", exc_info=True)

    # ── Agent-state mirror for LLM-aware idle watchdog ─────────────────────
    # AgentSession transitions through initializing → idle → listening →
    # thinking → speaking → idle (per turn). When the LLM is streaming a
    # response, state is "thinking" — and TTS hasn't started yet because
    # AgentSession's sentence tokenizer buffers tokens until a sentence
    # boundary. The idle watchdog only resets on TTSMetrics / EOUMetrics
    # / STTMetrics-with-audio, so during a slow "thinking" phase (observed
    # 15-20s on sarvam-105b "explain X" prompts) the watchdog could fire
    # and cancel the in-flight LLM response. Mirroring state here lets the
    # watchdog suppress its check while the agent is actively working.
    @session.on("agent_state_changed")
    def _on_agent_state(ev: Any) -> None:
        try:
            new_state = getattr(ev, "new_state", None)
            if isinstance(new_state, str):
                agent._agent_state = new_state
                logger.debug("[AGENT] state → %s", new_state)
        except Exception:
            logger.debug("[AGENT] failed to read agent_state_changed event", exc_info=True)

    # ── Mark call_log completed when the room ends ──────────────────────────
    # Without this hook, every call stays "initiated" in Mongo forever and the
    # frontend keeps polling /call-status, eventually saturating the API
    # (we shipped this exact bug earlier — 416 stuck rows).
    #
    # Also computes and stores the call duration. The Twilio status callback
    # updates `call_attempts` (used for campaigns), NOT `call_logs` — so without
    # this, the dashboard would show duration=0 and cost=$0 for every call.
    # Computing from created_at→ended_at gives a tight, accurate duration.
    async def _mark_call_completed() -> None:
        """Stamp call_log.ended_at + room_duration once the LiveKit room ends.

        Important: for PSTN calls, Twilio's `voice-status` webhook ALREADY
        finalizes `status` and `duration` (billable seconds, from answer to
        hangup) — and typically races ahead of THIS shutdown callback. So:

        - We do NOT filter by status (the old whitelist missed every PSTN
          call where Twilio's webhook fired first — observed 18/18 misses).
        - We NEVER clobber `status` or `duration` if Twilio's webhook
          already set them (those are the operator's source of truth).
        - We DO always stamp `ended_at` (otherwise time-window queries miss
          the call) and write `room_duration` as a separate wall-clock
          measurement (LiveKit room created → ended; useful for diagnosing
          ring/setup latency vs billable talk time).
        - For NON-PSTN rooms (web demo, browser test) where no call_log
          was ever created, we no-op with an info log instead of warning.
        """
        try:
            from app.config.database import Database
            db = Database.get_db()
            ended_at = datetime.now(timezone.utc)

            # pymongo's find_one/update_one are SYNC and would block the asyncio
            # event loop in this shutdown callback — which has historically
            # contributed to "entrypoint did not exit in time" warnings on
            # heavy-load workers. Wrap both round-trips in to_thread (same
            # pattern used by _lookup_twilio_call_sid and the transfer stamp).
            def _do_find():
                return db["call_logs"].find_one(
                    {"livekit_room": ctx.room.name},
                    {"_id": 1, "created_at": 1, "status": 1, "duration": 1},
                )
            log = await asyncio.to_thread(_do_find)
            if not log:
                logger.info(
                    "[AGENT] room %s ended — no matching call_log "
                    "(web demo or non-PSTN room; nothing to finalize)",
                    ctx.room.name,
                )
                return

            update_doc: Dict[str, Any] = {
                "ended_at": ended_at,
                "updated_at": ended_at,
            }
            # Wall-clock room duration — always written. Different from
            # Twilio's billable `duration` (answer→hangup); this is room
            # creation→teardown, including ring/setup.
            if log.get("created_at"):
                created = log["created_at"]
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                update_doc["room_duration"] = max(0, int((ended_at - created).total_seconds()))

            # Only finalize `status`/`duration` if Twilio's webhook HASN'T
            # already done so. Twilio is authoritative for billable duration;
            # we only fill in when it didn't fire (LiveKit-direct calls, or
            # an early-aborted call where Twilio never sent the final cb).
            current_status = (log.get("status") or "").lower()
            non_terminal = {"initiating", "initiated", "ringing", "queued", "in-progress"}
            if current_status in non_terminal or not current_status:
                update_doc["status"] = "completed"
                if "room_duration" in update_doc and not log.get("duration"):
                    update_doc["duration"] = update_doc["room_duration"]

            log_id = log["_id"]
            def _do_update():
                return db["call_logs"].update_one(
                    {"_id": log_id},
                    {"$set": update_doc},
                )
            await asyncio.to_thread(_do_update)
            logger.info(
                "[AGENT] room %s ended — call_log %s finalized "
                "(prev_status=%r, room_duration=%ss, set_terminal_status=%s)",
                ctx.room.name, log_id,
                current_status or "—",
                update_doc.get("room_duration"),
                "status" in update_doc,
            )
        except Exception:
            logger.exception("[AGENT] failed to finalize call_log")

    ctx.add_shutdown_callback(_mark_call_completed)

    # livekit-agents v1.5+ removed the auto_subscribe kwarg; default subscribes
    # to all audio tracks which is what we want.
    await ctx.connect()

    await session.start(agent=agent, room=ctx.room)

    # Idle-silence watchdog: hangs up the call if neither user nor bot has
    # spoken for IDLE_TIMEOUT seconds (default 10s). Prevents stuck rooms
    # when the caller leaves abruptly without saying goodbye. Stored on the
    # agent so a failed call-transfer can re-arm it; _cancel_watchdog cancels
    # whatever it currently points to on natural shutdown.
    agent._watchdog_task = asyncio.create_task(agent.idle_watchdog())

    async def _cancel_watchdog() -> None:
        for attr in ("_watchdog_task", "_transfer_task"):
            t = getattr(agent, attr, None)
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug("[AGENT] %s cancel cleanup", attr, exc_info=True)

    ctx.add_shutdown_callback(_cancel_watchdog)

    # Greeting via session.say(): goes straight to TTS, bypassing the LLM.
    # Saves the LLM round-trip on greeting (~300-500ms) AND makes greeting
    # deterministic — no risk of the LLM "creatively" rephrasing the configured
    # greeting.
    #
    # No LLM cache warmup: Sarvam has no equivalent of OpenAI's prompt_cache_key,
    # so the concurrent warmup task that used to fire here is gone. First-turn
    # TTFT pays the full prompt-processing cost (~1.5-3s on sarvam-105b with
    # /nothink) every cold call. Mitigations are prompt trimming, not warming.
    greeting = config.get("greeting") or "Hello! How can I help you today?"
    if config.get("resumed_after_failed_transfer"):
        # We got here because a transfer to a human didn't connect (no-answer/
        # busy/failed) and Twilio's <Dial action> webhook re-bridged the caller
        # back to a fresh agent. Open with an apology instead of the normal
        # greeting — the prior conversation context is gone (new agent process).
        greeting = (
            "I'm sorry, I wasn't able to reach a team member right now. "
            "Is there anything else I can help you with?"
        )
    await session.say(greeting, allow_interruptions=True)


if __name__ == "__main__":
    agent_name = os.getenv("LIVEKIT_AGENT_NAME", "convis-agent")
    # num_idle_processes=1 keeps one fully-loaded worker process always idle and
    # ready to accept the next job. Without it, LiveKit spawns a fresh process
    # per call → every call pays Silero VAD load (~500ms via prewarm), module
    # imports (~200ms), and provider client init (~100ms) before greeting can
    # play. With num_idle_processes=1, that boot cost happens once, between
    # calls, while the prior call is still wrapping up. Memory cost: ~512 MB
    # idle for the warm process. Fits easily in our 4 GB Fargate task.
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=agent_name,
            num_idle_processes=1,
        )
    )
