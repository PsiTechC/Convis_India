"""
Smoke test for the LiveKit agent worker pipeline wiring.

Asserts that `app.services.livekit.agent_worker.entrypoint` wires the
Deepgram STT + OpenAI LLM + ElevenLabs TTS stack with the values pulled from
the assistant config, and that the session starts + sends a greeting.

LiveKit SDKs are stubbed in tests/livekit/conftest.py — this test just inspects
the constructor kwargs captured on those stubs.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _build_ctx(metadata: dict) -> SimpleNamespace:
    """Build a stub JobContext that carries metadata via ctx.job.metadata."""
    ctx = SimpleNamespace()
    ctx.job = SimpleNamespace(metadata=json.dumps(metadata))
    ctx.room = SimpleNamespace(metadata="", name="test-room")
    ctx.connect = AsyncMock()
    ctx.add_shutdown_callback = lambda *a, **k: None
    ctx.proc = SimpleNamespace(userdata={})  # tests that need a prewarmed VAD override this
    return ctx


def test_entrypoint_wires_deepgram_openai_elevenlabs(monkeypatch):
    from app.services.livekit import agent_worker

    captured = {}

    def make_stub(kind):
        def _ctor(*args, **kwargs):
            captured[kind] = kwargs
            stub = MagicMock(name=kind)
            stub.kwargs = kwargs
            return stub
        return _ctor

    # Capture constructor kwargs on each plugin
    monkeypatch.setattr(agent_worker.deepgram, "STT", make_stub("stt"))
    monkeypatch.setattr(agent_worker.openai, "LLM", make_stub("llm"))
    monkeypatch.setattr(agent_worker.elevenlabs, "TTS", make_stub("tts"))
    monkeypatch.setattr(
        agent_worker.elevenlabs,
        "VoiceSettings",
        lambda **kw: SimpleNamespace(**kw),
    )
    monkeypatch.setattr(
        agent_worker.silero.VAD,
        "load",
        lambda *a, **k: MagicMock(name="vad"),
    )

    # Capture AgentSession wiring + track that start + generate_reply ran
    session_started = {}
    session_greeted = {}

    class FakeSession:
        def __init__(self, **kwargs):
            captured["session"] = kwargs

        async def start(self, **kwargs):
            session_started.update(kwargs)

        async def generate_reply(self, **kwargs):
            session_greeted.update(kwargs)

        async def say(self, *a, **k):
            session_greeted.setdefault("said", a[0] if a else None)

        def on(self, *a, **k):
            return lambda fn: fn

    monkeypatch.setattr(agent_worker, "AgentSession", FakeSession)

    metadata = {
        "assistant_id": "abc123",
        "name": "Billing Bot",
        "system_message": "You are a billing assistant.",
        "greeting": "Hi, this is billing.",
        "asr_model": "nova-2",
        "asr_language": "en",
        "asr_keywords": ["refund", "invoice"],
        "llm_model": "gpt-4o-mini",
        "temperature": 0.3,
        "tts_voice": "21m00Tcm4TlvDq8ikWAM",
        "tts_model": "eleven_flash_v2_5",
        "tts_stability": 0.4,
        "tts_similarity_boost": 0.8,
        "tts_style": 0.1,
        "tts_speed": 1.1,
    }

    ctx = _build_ctx(metadata)
    asyncio.run(agent_worker.entrypoint(ctx))

    # STT: Deepgram streaming with low-latency knobs
    assert captured["stt"]["model"] == "nova-2"
    assert captured["stt"]["language"] == "en"
    assert captured["stt"]["interim_results"] is True
    assert captured["stt"]["smart_format"] is True
    assert captured["stt"]["no_delay"] is True, (
        "no_delay must be True — without it Deepgram buffers final transcripts "
        "for smart-formatting, costing ~150 ms per turn"
    )
    assert captured["stt"]["endpointing_ms"] == 300, (
        "endpointing_ms default of 300 ms is the documented latency target. "
        "Bumping this up silently re-introduces dead-air at every turn end."
    )
    assert captured["stt"]["keywords"] == ["refund", "invoice"]

    # LLM: OpenAI with model/temperature from config
    assert captured["llm"]["model"] == "gpt-4o-mini"
    assert captured["llm"]["temperature"] == 0.3

    # TTS: ElevenLabs with voice/model + voice_settings bundle
    assert captured["tts"]["voice_id"] == "21m00Tcm4TlvDq8ikWAM"
    assert captured["tts"]["model"] == "eleven_flash_v2_5"
    vs = captured["tts"]["voice_settings"]
    assert vs.stability == 0.4
    assert vs.similarity_boost == 0.8
    assert vs.style == 0.1
    assert vs.speed == 1.1

    # Session: all four components + low-latency response/interruption tuning
    assert {"vad", "stt", "llm", "tts"}.issubset(captured["session"].keys())
    assert captured["session"]["min_endpointing_delay"] == 0.3
    assert captured["session"]["min_interruption_duration"] == 0.4
    ctx.connect.assert_awaited_once()
    assert "room" in session_started
    assert "Hi, this is billing." in session_greeted["instructions"]


def test_entrypoint_defaults_when_config_omits_fields(monkeypatch):
    """If the assistant config omits optional fields, entrypoint should fall back
    to the documented defaults (nova-2, gpt-4o-mini, eleven_flash_v2_5, alloy)."""
    from app.services.livekit import agent_worker

    captured = {}

    def make_stub(kind):
        def _ctor(*args, **kwargs):
            captured[kind] = kwargs
            return MagicMock()
        return _ctor

    monkeypatch.setattr(agent_worker.deepgram, "STT", make_stub("stt"))
    monkeypatch.setattr(agent_worker.openai, "LLM", make_stub("llm"))
    monkeypatch.setattr(agent_worker.elevenlabs, "TTS", make_stub("tts"))
    monkeypatch.setattr(
        agent_worker.elevenlabs,
        "VoiceSettings",
        lambda **kw: SimpleNamespace(**kw),
    )
    monkeypatch.setattr(
        agent_worker.silero.VAD,
        "load",
        lambda *a, **k: MagicMock(),
    )

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        async def start(self, **kwargs):
            pass

        async def generate_reply(self, **kwargs):
            pass

        async def say(self, *a, **k):
            pass

        def on(self, *a, **k):
            return lambda fn: fn

    monkeypatch.setattr(agent_worker, "AgentSession", FakeSession)

    ctx = _build_ctx({"system_message": "You are helpful."})
    asyncio.run(agent_worker.entrypoint(ctx))

    assert captured["stt"]["model"] == "nova-2"
    assert captured["stt"]["language"] == "en"
    assert captured["stt"]["keywords"] is None
    assert captured["llm"]["model"] == "gpt-4o-mini"
    assert captured["tts"]["voice_id"] == "alloy"
    assert captured["tts"]["model"] == "eleven_flash_v2_5"


def test_entrypoint_uses_prewarmed_vad_when_available(monkeypatch):
    """Prewarmed VAD on ctx.proc.userdata must be reused — never reloaded mid-job.

    Reloading on every call would re-incur the ~500 ms ONNX init we paid in
    prewarm_fnc, defeating the whole point.
    """
    from app.services.livekit import agent_worker

    captured = {}
    monkeypatch.setattr(agent_worker.deepgram, "STT", lambda **kw: MagicMock())
    monkeypatch.setattr(agent_worker.openai, "LLM", lambda **kw: MagicMock())
    monkeypatch.setattr(agent_worker.elevenlabs, "TTS", lambda **kw: MagicMock())
    monkeypatch.setattr(
        agent_worker.elevenlabs, "VoiceSettings", lambda **kw: SimpleNamespace(**kw),
    )

    # If silero.VAD.load is called, the prewarm cache wasn't honored — fail.
    def boom(*a, **k):
        raise AssertionError("silero.VAD.load must NOT run when prewarm cache is set")
    monkeypatch.setattr(agent_worker.silero.VAD, "load", boom)

    class FakeSession:
        def __init__(self, **kwargs):
            captured["session"] = kwargs
        async def start(self, **k): pass
        async def generate_reply(self, **k): pass
        async def say(self, *a, **k): pass
        def on(self, *a, **k): return lambda fn: fn
    monkeypatch.setattr(agent_worker, "AgentSession", FakeSession)

    prewarmed_vad = MagicMock(name="prewarmed_vad")
    proc = SimpleNamespace(userdata={"vad": prewarmed_vad})
    ctx = _build_ctx({"system_message": "x"})
    ctx.proc = proc

    asyncio.run(agent_worker.entrypoint(ctx))

    assert captured["session"]["vad"] is prewarmed_vad


def test_prewarm_loads_vad_into_userdata(monkeypatch):
    """prewarm_fnc must populate proc.userdata['vad'] — that's the contract
    entrypoint relies on to skip VAD load on every job."""
    from app.services.livekit import agent_worker

    sentinel = MagicMock(name="loaded_vad")
    monkeypatch.setattr(agent_worker.silero.VAD, "load", lambda *a, **k: sentinel)

    proc = SimpleNamespace(userdata={})
    agent_worker.prewarm(proc)

    assert proc.userdata["vad"] is sentinel


def test_per_assistant_latency_overrides(monkeypatch):
    """Operators can tune endpointing per assistant via room metadata. Defaults
    serve the typical case; this confirms overrides actually flow through."""
    from app.services.livekit import agent_worker

    captured = {}
    monkeypatch.setattr(agent_worker.deepgram, "STT", lambda **kw: captured.setdefault("stt", kw) or MagicMock())
    monkeypatch.setattr(agent_worker.openai, "LLM", lambda **kw: MagicMock())
    monkeypatch.setattr(agent_worker.elevenlabs, "TTS", lambda **kw: MagicMock())
    monkeypatch.setattr(
        agent_worker.elevenlabs, "VoiceSettings", lambda **kw: SimpleNamespace(**kw),
    )
    monkeypatch.setattr(agent_worker.silero.VAD, "load", lambda *a, **k: MagicMock())

    class FakeSession:
        def __init__(self, **kwargs):
            captured["session"] = kwargs
        async def start(self, **k): pass
        async def generate_reply(self, **k): pass
        async def say(self, *a, **k): pass
        def on(self, *a, **k): return lambda fn: fn
    monkeypatch.setattr(agent_worker, "AgentSession", FakeSession)

    ctx = _build_ctx({
        "system_message": "x",
        # Older speakers / clinical use case — wait longer before barging
        "asr_endpointing_ms": 600,
        "min_endpointing_delay": 0.6,
        "min_interruption_duration": 0.7,
    })
    asyncio.run(agent_worker.entrypoint(ctx))

    assert captured["stt"]["endpointing_ms"] == 600
    assert captured["session"]["min_endpointing_delay"] == 0.6
    assert captured["session"]["min_interruption_duration"] == 0.7


def test_entrypoint_raises_without_config():
    """No metadata anywhere → entrypoint must fail loudly."""
    from app.services.livekit import agent_worker

    ctx = SimpleNamespace(
        job=SimpleNamespace(metadata=""),
        room=SimpleNamespace(metadata="", name="r"),
        connect=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="No assistant config"):
        asyncio.run(agent_worker.entrypoint(ctx))
