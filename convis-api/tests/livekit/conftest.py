"""Pytest config for the LiveKit migration tests.

These tests do not install the `livekit*` SDKs — instead we stub enough of the
surface area (`livekit.api.*`, `livekit.agents.*`, plugins, `rtc`) that our
wrapper modules import cleanly and we can assert on their behaviour.

This is deliberate: it lets us validate that our adapters call the SDK with the
correct arguments without requiring ~500MB of audio/ML dependencies to be
installed in a dev environment.

Real end-to-end tests (agent connecting to LiveKit Cloud, placing real calls)
live outside this directory and require actual credentials — see
`deployment-docs/LIVEKIT_SIP_SETUP.md`.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# Minimum env so app.config.settings doesn't blow up on import.
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/test")
os.environ.setdefault("DATABASE_NAME", "convis_test")
os.environ.setdefault("EMAIL_USER", "test@example.com")
os.environ.setdefault("EMAIL_PASS", "password")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-do-not-use-in-prod")

# Ensure `app.*` is importable when pytest is invoked from repo root or the
# convis-api directory.
CONVIS_API = Path(__file__).resolve().parents[2]
if str(CONVIS_API) not in sys.path:
    sys.path.insert(0, str(CONVIS_API))


# ──────────────────────────────────────────────────────────────────────────
#  Stub the LiveKit SDK so the modules under test import without the real deps.
# ──────────────────────────────────────────────────────────────────────────

def _install_livekit_stubs() -> None:
    if "livekit" in sys.modules:
        return

    livekit = types.ModuleType("livekit")
    sys.modules["livekit"] = livekit

    # ── livekit.api ──
    api_mod = types.ModuleType("livekit.api")

    class _Grants:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class AccessToken:
        """Minimal faithful reimplementation of livekit.api.AccessToken.to_jwt
        using PyJWT (which *is* installed). We only use this for test assertions."""
        def __init__(self, api_key: str, api_secret: str):
            self._api_key = api_key
            self._api_secret = api_secret
            self._identity = None
            self._name = None
            self._metadata = None
            self._grants: _Grants | None = None
            self._ttl = 3600

        def with_identity(self, identity: str):
            self._identity = identity
            return self

        def with_name(self, name: str):
            self._name = name
            return self

        def with_metadata(self, metadata: str):
            self._metadata = metadata
            return self

        def with_grants(self, grants: _Grants):
            self._grants = grants
            return self

        def with_ttl(self, ttl_seconds: int):
            self._ttl = ttl_seconds
            return self

        def to_jwt(self) -> str:
            import time
            from datetime import timedelta

            import jwt as pyjwt

            now = int(time.time())
            # The real SDK accepts int (legacy) or timedelta (current). Match.
            ttl_seconds = (
                int(self._ttl.total_seconds())
                if isinstance(self._ttl, timedelta)
                else int(self._ttl)
            )
            claims = {
                "iss": self._api_key,
                "sub": self._identity,
                "nbf": now,
                "exp": now + ttl_seconds,
            }
            if self._name:
                claims["name"] = self._name
            if self._metadata:
                claims["metadata"] = self._metadata
            if self._grants is not None:
                claims["video"] = {
                    "room": getattr(self._grants, "room", None),
                    "roomJoin": getattr(self._grants, "room_join", False),
                    "canPublish": getattr(self._grants, "can_publish", False),
                    "canSubscribe": getattr(self._grants, "can_subscribe", False),
                    "canPublishData": getattr(self._grants, "can_publish_data", False),
                }
            return pyjwt.encode(claims, self._api_secret, algorithm="HS256")

    def VideoGrants(**kwargs):
        return _Grants(**kwargs)

    # Request dataclasses — the real SDK uses betterproto messages; tests just
    # need to introspect attributes, so a plain namespace is fine.
    class _Request:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class CreateRoomRequest(_Request): ...
    class DeleteRoomRequest(_Request): ...
    class CreateAgentDispatchRequest(_Request): ...
    class CreateSIPParticipantRequest(_Request): ...

    class LiveKitAPI:
        """Stub — tests replace this with their own MagicMock via monkeypatch."""
        def __init__(self, url: str, api_key: str, api_secret: str):
            self.url = url
            self.api_key = api_key
            self.api_secret = api_secret
            self.room = MagicMock()
            self.agent_dispatch = MagicMock()
            self.sip = MagicMock()

        async def aclose(self):
            return None

    api_mod.AccessToken = AccessToken
    api_mod.VideoGrants = VideoGrants
    api_mod.CreateRoomRequest = CreateRoomRequest
    api_mod.DeleteRoomRequest = DeleteRoomRequest
    api_mod.CreateAgentDispatchRequest = CreateAgentDispatchRequest
    api_mod.CreateSIPParticipantRequest = CreateSIPParticipantRequest
    api_mod.LiveKitAPI = LiveKitAPI
    sys.modules["livekit.api"] = api_mod
    livekit.api = api_mod

    # ── livekit.agents + plugins (only needed by agent_worker import path) ──
    agents_mod = types.ModuleType("livekit.agents")
    cli_mod = types.ModuleType("livekit.agents.cli")
    cli_mod.run_app = lambda *a, **k: None
    agents_mod.cli = cli_mod

    class _Agent:
        def __init__(self, instructions: str = "", chat_ctx=None):
            # `chat_ctx` accepted but unused in tests — production passes it
            # for the conversation-memory feature (second system message);
            # tests only assert on `instructions`.
            self.instructions = instructions
            self.chat_ctx = chat_ctx

    class _AgentSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def start(self, **kwargs):
            return None

        async def generate_reply(self, **kwargs):
            return None

    class _JobContext:
        def __init__(self):
            self.room = MagicMock()
            self.job = MagicMock()

        async def connect(self, **kwargs):
            return None

    class _WorkerOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _ChatContext:
        """Minimal stub for livekit.agents.ChatContext, just enough for the
        conversation-memory chat_ctx-split code path in agent_worker to
        construct. Tests don't currently assert on its contents; if a future
        test does, expand this stub."""
        def __init__(self):
            self._items = []

        @classmethod
        def empty(cls):
            return cls()

        def add_message(self, *, role, content):
            self._items.append({"role": role, "content": content})
            return self

    agents_mod.Agent = _Agent
    agents_mod.AgentSession = _AgentSession
    agents_mod.ChatContext = _ChatContext
    agents_mod.JobContext = _JobContext
    agents_mod.WorkerOptions = _WorkerOptions
    # @function_tool is used as a bare decorator on ConvisAgent's tool methods
    # (search_knowledge_base, end_call, transfer_to_agent). A passthrough is
    # enough for tests — the entrypoint tests don't invoke the tools.
    agents_mod.function_tool = lambda fn: fn
    sys.modules["livekit.agents"] = agents_mod
    sys.modules["livekit.agents.cli"] = cli_mod
    livekit.agents = agents_mod

    rtc_mod = types.ModuleType("livekit.rtc")
    rtc_mod.AutoSubscribe = types.SimpleNamespace(AUDIO_ONLY="audio_only", SUBSCRIBE_ALL="all")
    sys.modules["livekit.rtc"] = rtc_mod
    livekit.rtc = rtc_mod

    # Plugins — agent_worker imports these at module load time.
    plugins_pkg = types.ModuleType("livekit.plugins")
    sys.modules["livekit.plugins"] = plugins_pkg

    class _StubPlugin:
        """Collects constructor kwargs so tests can assert wiring."""
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    for name in ("deepgram", "openai", "elevenlabs", "silero"):
        sub = types.ModuleType(f"livekit.plugins.{name}")
        sub.STT = _StubPlugin
        sub.LLM = _StubPlugin
        sub.TTS = _StubPlugin
        sub.VAD = types.SimpleNamespace(load=lambda *a, **k: _StubPlugin())
        sub.VoiceSettings = _StubPlugin
        sys.modules[f"livekit.plugins.{name}"] = sub
        setattr(plugins_pkg, name, sub)


_install_livekit_stubs()


# ──────────────────────────────────────────────────────────────────────────
#  Disable rate limiter for tests.
#  @limiter.limit introspects the Request object — real handlers get one from
#  FastAPI, but unit tests that call handlers directly pass a MagicMock which
#  slowapi rejects. We disable the limiter at the module level for tests.
#  Rate-limit behavior itself is verified by integration tests that go through
#  the FastAPI test client (where Request is real).
# ──────────────────────────────────────────────────────────────────────────
try:
    from app.middleware.rate_limiter import limiter as _rate_limiter
    _rate_limiter.enabled = False
except Exception:
    pass


# ──────────────────────────────────────────────────────────────────────────
#  Fake Mongo database. MagicMock's __getitem__ collapses all subscript keys
#  onto a single child mock, so `db["a"]` and `db["b"]` alias and later
#  `.return_value =` calls clobber earlier ones. FakeDB fixes that.
# ──────────────────────────────────────────────────────────────────────────


class FakeDB:
    """Dict-like Mongo stand-in — one MagicMock per collection name."""

    def __init__(self) -> None:
        self._collections: dict[str, MagicMock] = {}

    def __getitem__(self, name: str) -> MagicMock:
        if name not in self._collections:
            self._collections[name] = MagicMock(name=f"Collection({name!r})")
        return self._collections[name]

    def __contains__(self, name: str) -> bool:
        return name in self._collections


import pytest  # noqa: E402 — after sys.path setup


@pytest.fixture
def fake_db():
    return FakeDB()
