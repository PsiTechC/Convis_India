"""Integrity tests — the migration left the codebase in a consistent state.

These tests don't mock anything app-internal; they just verify invariants about
the repo itself (modules removed, new ones present, no stale imports).
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONVIS_API = REPO_ROOT / "convis-api"
APP_DIR = CONVIS_API / "app"

DELETED_MODULES = {
    "app.services.webrtc",
    "app.routes.webrtc",
    "app.services.call_handlers.optimized_stream_handler",
    "app.services.call_handlers.ultra_low_latency_handler",
    "app.services.call_handlers.streaming_asr_handler",
    "app.services.call_handlers.streaming_llm_handler",
    "app.services.call_handlers.streaming_tts_handler",
    "app.services.call_handlers.offline_asr_handler",
    "app.services.call_handlers.offline_tts_handler",
    "app.services.call_handlers.custom_provider_stream",
    "app.services.call_handlers.elevenlabs_websocket_tts",
    "app.services.call_handlers.sarvam_streaming_tts",
    "app.utils.custom_provider_handler",
}

NEW_MODULES = {
    "app.services.livekit.agent_worker",
    "app.services.livekit.assistant_config",
    "app.services.livekit.sip_service",
    "app.services.livekit.tokens",
    "app.routes.livekit.routes",
}


def test_deleted_modules_are_gone():
    for mod in DELETED_MODULES:
        rel = mod.replace(".", "/")
        as_file = APP_DIR / (rel.removeprefix("app/") + ".py")
        as_pkg = APP_DIR / rel.removeprefix("app/")
        assert not as_file.exists(), f"{mod} still exists at {as_file}"
        assert not as_pkg.exists(), f"{mod} still exists as package at {as_pkg}"


def test_new_modules_present():
    for mod in NEW_MODULES:
        rel = mod.replace(".", "/")
        p = APP_DIR / (rel.removeprefix("app/") + ".py")
        assert p.exists(), f"Expected new module missing: {p}"


def test_no_stale_imports_in_app():
    """Grep-style scan: no source file under app/ references a deleted module."""
    patterns = [re.compile(re.escape(mod)) for mod in DELETED_MODULES]
    offenders = []
    for py in APP_DIR.rglob("*.py"):
        text = py.read_text()
        for pat in patterns:
            if pat.search(text):
                offenders.append((py, pat.pattern))
    assert not offenders, f"Stale imports found: {offenders}"


def test_main_router_includes_livekit_not_webrtc():
    main_src = (APP_DIR / "main.py").read_text()
    assert "/api/livekit" in main_src, "livekit router not mounted"
    assert "/api/webrtc" not in main_src, "old webrtc prefix still mounted"
    assert "from app.routes.livekit import" in main_src
    assert "from app.routes.webrtc" not in main_src


def test_requirements_contains_livekit_not_offline():
    req = (CONVIS_API / "requirements.txt").read_text()
    assert "livekit>=" in req
    assert "livekit-api>=" in req
    assert "livekit-agents>=" in req
    assert "livekit-plugins-deepgram" in req
    assert "livekit-plugins-openai" in req
    assert "livekit-plugins-elevenlabs" in req
    assert "livekit-plugins-silero" in req
    assert "faster-whisper" not in req, "offline whisper dep should be gone"
    assert "piper-tts" not in req, "offline piper dep should be gone"
    # silero-vad standalone (not the livekit plugin) should be gone too
    assert "silero-vad" not in req


def test_docker_compose_has_livekit_agent():
    import yaml

    dc = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    assert "livekit-agent" in dc["services"], "livekit-agent service missing"
    agent = dc["services"]["livekit-agent"]
    assert any("LIVEKIT_URL" in e for e in agent["environment"])
    assert any("DEEPGRAM_API_KEY" in e for e in agent["environment"])
    assert any("ELEVENLABS_API_KEY" in e for e in agent["environment"])
    api = dc["services"]["api"]
    assert any("LIVEKIT_URL" in e for e in api["environment"])


def test_frontend_package_has_livekit_client():
    import json

    pkg = json.loads((REPO_ROOT / "convis-web" / "package.json").read_text())
    assert "livekit-client" in pkg["dependencies"]


def test_all_new_modules_import_cleanly():
    for mod in NEW_MODULES:
        importlib.import_module(mod)
