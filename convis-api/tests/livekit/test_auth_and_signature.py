"""
Regression tests for the security fixes.

Bugs guarded:
- LoginResponse no longer leaks an obfuscated isAdmin field
- JWT now carries role; auth dependency returns role from token, never DB
- Settings refuses to load with no JWT_SECRET
- Twilio signature verification dependency rejects unsigned/forged requests
- Dashboard.should_include_call uses tz-aware UTC and excludes undated rows
  from bounded timeframes
- local_embeddings raises on failure instead of silently returning []
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from bson import ObjectId
from fastapi import HTTPException


# ─── Login response shape ───────────────────────────────────────────────────


def test_login_response_model_has_no_obfuscated_admin_field():
    from app.models.login import LoginResponse
    fields = set(LoginResponse.model_fields.keys())
    suspicious = [f for f in fields if "amdin" in f or "683ed29d" in f]
    assert suspicious == [], f"Obfuscated admin field still present: {suspicious}"
    assert "role" in fields


def test_login_response_role_literal_rejects_unknown_values():
    from pydantic import ValidationError

    from app.models.login import LoginResponse
    with pytest.raises(ValidationError):
        LoginResponse(
            redirectUrl="/x", clientId="abc", role="superuser", token="t"
        )


# ─── JWT carries role, auth derives role from token ─────────────────────────


@pytest.mark.asyncio
async def test_get_current_user_uses_jwt_role_not_db():
    from app.config.settings import settings
    from app.utils.auth import get_current_user

    user_id = ObjectId()
    token = pyjwt.encode(
        {
            "clientId": str(user_id),
            "email": "u@example.com",
            "role": "admin",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )

    fake_collection = MagicMock()
    # DB user doc says role=user, but JWT says admin → JWT wins
    fake_collection.find_one.return_value = {"_id": user_id, "role": "user"}

    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_collection

    with patch("app.utils.auth.Database.get_db", return_value=fake_db):
        user = await get_current_user(authorization=f"Bearer {token}")

    assert user["token_role"] == "admin"


@pytest.mark.asyncio
async def test_get_current_user_defaults_role_to_user_for_legacy_tokens():
    """Old tokens without a role claim must NOT silently become admin."""
    from app.config.settings import settings
    from app.utils.auth import get_current_user

    user_id = ObjectId()
    token = pyjwt.encode(
        {
            "clientId": str(user_id),
            "email": "u@example.com",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )

    fake_collection = MagicMock()
    # DB says admin, but JWT lacks role → must default to user
    fake_collection.find_one.return_value = {"_id": user_id, "role": "admin"}

    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_collection

    with patch("app.utils.auth.Database.get_db", return_value=fake_db):
        user = await get_current_user(authorization=f"Bearer {token}")

    assert user["token_role"] == "user", "Legacy tokens must NEVER be treated as admin"


def test_require_admin_helper():
    from app.utils.auth import require_admin

    require_admin({"token_role": "admin"})  # no raise

    with pytest.raises(HTTPException) as exc:
        require_admin({"token_role": "user"})
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException):
        require_admin({})  # no token_role at all → reject


# ─── Twilio signature verification ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_twilio_signature_rejects_missing_header(monkeypatch):
    from app.config.settings import settings
    from app.utils.twilio_signature import verify_twilio_signature

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "twilio_auth_token", "test-token")

    request = MagicMock()
    request.headers = {}  # no x-twilio-signature
    request.url.path = "/api/twilio-webhooks/call-status"
    request.url.query = ""

    with pytest.raises(HTTPException) as exc:
        await verify_twilio_signature(request)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_twilio_signature_rejects_forged(monkeypatch):
    from app.config.settings import settings
    from app.utils.twilio_signature import verify_twilio_signature

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "twilio_auth_token", "real-token")
    monkeypatch.setattr(settings, "api_base_url", "https://api.example.com")

    async def fake_form():
        m = MagicMock()
        m.multi_items.return_value = [("CallSid", "CA123"), ("From", "+15551110000")]
        return m

    request = MagicMock()
    request.headers = {"x-twilio-signature": "totally-fake-signature"}
    request.url.path = "/api/twilio-webhooks/call-status"
    request.url.query = ""
    request.form = fake_form

    with pytest.raises(HTTPException) as exc:
        await verify_twilio_signature(request)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_twilio_signature_503_when_auth_token_missing(monkeypatch):
    from app.config.settings import settings
    from app.utils.twilio_signature import verify_twilio_signature

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "twilio_auth_token", None)

    # No provider_connections collection available in this unit test, so the
    # per-account token lookup yields nothing and, with no env token either,
    # there's no candidate token at all → 503.
    monkeypatch.setattr(settings, "twilio_account_sid", None)
    from starlette.datastructures import FormData

    request = MagicMock()
    request.headers = {"x-twilio-signature": "x"}
    request.url.path = "/api/twilio-webhooks/call-status"
    request.url.query = ""
    request.query_params = {}
    request.form = AsyncMock(return_value=FormData())
    request.state = MagicMock()

    with pytest.raises(HTTPException) as exc:
        await verify_twilio_signature(request)
    # Server misconfig = 503, not 403; we shouldn't accept the webhook either.
    assert exc.value.status_code == 503


# ─── Dashboard timeframe correctness ────────────────────────────────────────


def test_should_include_call_excludes_undated_rows_from_bounded_timeframes():
    from app.routes.dashboard import should_include_call
    # Bounded timeframes: undated rows are dropped (no silent inflation)
    assert should_include_call(None, None, "last_7d") is False
    assert should_include_call(None, None, "last_30d") is False
    assert should_include_call(None, None, "current_year") is False
    # "total" includes everything
    assert should_include_call(None, None, "total") is True


def test_should_include_call_handles_naive_and_aware_uniformly():
    from app.routes.dashboard import should_include_call

    now_aware = datetime.now(timezone.utc)
    inside = now_aware - timedelta(days=3)
    outside = now_aware - timedelta(days=10)

    # Both naive and aware should give the same answer for the same instant
    inside_naive = inside.replace(tzinfo=None)
    outside_naive = outside.replace(tzinfo=None)

    assert should_include_call(inside, None, "last_7d") is True
    assert should_include_call(inside_naive, None, "last_7d") is True
    assert should_include_call(outside, None, "last_7d") is False
    assert should_include_call(outside_naive, None, "last_7d") is False


# ─── Embeddings: fail loud, not silent ──────────────────────────────────────


def test_local_embeddings_raises_on_provider_failure(monkeypatch):
    import app.utils.local_embeddings as le

    def boom(*a, **k):
        raise RuntimeError("openai 429")

    monkeypatch.setattr(le, "_create_embeddings_openai", boom)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")

    with pytest.raises(le.EmbeddingProviderError):
        le.create_embeddings_auto(["hello"], api_key="sk-test")


def test_local_embeddings_returns_empty_for_empty_input():
    import app.utils.local_embeddings as le
    assert le.create_embeddings_auto([]) == []
