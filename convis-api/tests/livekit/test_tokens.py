"""Unit tests for app.services.livekit.tokens."""
from __future__ import annotations

import jwt as pyjwt
import pytest


def _configure(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_api_key", "APItest")
    monkeypatch.setattr(settings, "livekit_api_secret", "super-secret-value")


def test_missing_config_raises():
    from app.config.settings import settings
    from app.services.livekit import tokens

    # Snapshot and clear
    snap = (settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret)
    try:
        settings.livekit_url = None
        settings.livekit_api_key = None
        settings.livekit_api_secret = None
        with pytest.raises(tokens.LiveKitNotConfigured):
            tokens.mint_participant_token(room_name="r", identity="u")
    finally:
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret = snap


def test_token_is_valid_jwt_with_correct_claims(monkeypatch):
    from app.services.livekit import tokens

    _configure(monkeypatch)
    token = tokens.mint_participant_token(
        room_name="web-abc123",
        identity="user-42",
        name="Jane Doe",
        metadata='{"foo":"bar"}',
        ttl_seconds=120,
    )
    claims = pyjwt.decode(token, "super-secret-value", algorithms=["HS256"])
    assert claims["sub"] == "user-42"
    assert claims["name"] == "Jane Doe"
    assert claims["metadata"] == '{"foo":"bar"}'
    assert claims["video"]["room"] == "web-abc123"
    assert claims["video"]["roomJoin"] is True
    assert claims["video"]["canPublish"] is True
    assert claims["video"]["canSubscribe"] is True


def test_token_rejects_wrong_secret(monkeypatch):
    from app.services.livekit import tokens

    _configure(monkeypatch)
    token = tokens.mint_participant_token(room_name="r", identity="u")
    with pytest.raises(pyjwt.InvalidSignatureError):
        pyjwt.decode(token, "wrong-secret", algorithms=["HS256"])


def test_livekit_api_client_requires_config():
    from app.config.settings import settings
    from app.services.livekit import tokens

    snap = (settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret)
    try:
        settings.livekit_url = None
        with pytest.raises(tokens.LiveKitNotConfigured):
            tokens.livekit_api_client()
    finally:
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret = snap
