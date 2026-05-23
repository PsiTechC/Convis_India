"""Unit tests for app.services.livekit.sip_service."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _configure(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_api_key", "APItest")
    monkeypatch.setattr(settings, "livekit_api_secret", "secret")
    monkeypatch.setattr(settings, "livekit_agent_name", "convis-agent")


def _mock_lk_api():
    fake = MagicMock()
    fake.room.create_room = AsyncMock()
    fake.room.delete_room = AsyncMock()
    fake.agent_dispatch.create_dispatch = AsyncMock()
    fake.sip.create_sip_participant = AsyncMock()
    fake.aclose = AsyncMock()
    return fake


def test_generate_room_name_unique_and_prefixed():
    from app.services.livekit.sip_service import generate_room_name

    names = [generate_room_name(prefix="pstn-out") for _ in range(50)]
    assert len(set(names)) == 50
    assert all(n.startswith("pstn-out-") for n in names)


@pytest.mark.asyncio
async def test_create_room_with_agent_calls_both_endpoints(monkeypatch):
    _configure(monkeypatch)
    from app.services.livekit import sip_service

    fake = _mock_lk_api()
    monkeypatch.setattr(sip_service, "livekit_api_client", lambda: fake)

    room = await sip_service.create_room_with_agent(
        room_name="web-abc",
        assistant_config={"assistant_id": "a1", "system_message": "hi"},
        metadata_extra={"source": "web"},
    )

    assert room == "web-abc"
    fake.room.create_room.assert_awaited_once()
    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    fake.aclose.assert_awaited_once()

    # Dispatch targets the configured agent name and correct room.
    dispatch_req = fake.agent_dispatch.create_dispatch.await_args.args[0]
    assert dispatch_req.agent_name == "convis-agent"
    assert dispatch_req.room == "web-abc"
    # Metadata is JSON-encoded and includes our extras.
    import json
    meta = json.loads(dispatch_req.metadata)
    assert meta["assistant_id"] == "a1"
    assert meta["source"] == "web"


@pytest.mark.asyncio
async def test_create_room_closes_api_client_on_error(monkeypatch):
    _configure(monkeypatch)
    from app.services.livekit import sip_service

    fake = _mock_lk_api()
    fake.room.create_room.side_effect = RuntimeError("boom")
    monkeypatch.setattr(sip_service, "livekit_api_client", lambda: fake)

    with pytest.raises(RuntimeError, match="boom"):
        await sip_service.create_room_with_agent(
            room_name="r", assistant_config={"assistant_id": "a", "system_message": "h"}
        )
    fake.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_dial_outbound_sip_requires_trunk(monkeypatch):
    _configure(monkeypatch)
    from app.config.settings import settings
    from app.services.livekit import sip_service

    monkeypatch.setattr(settings, "livekit_sip_outbound_trunk_id", None)
    with pytest.raises(sip_service.LiveKitNotConfigured):
        await sip_service.dial_outbound_sip(
            room_name="r", phone_number="+15551234567"
        )


@pytest.mark.asyncio
async def test_dial_outbound_sip_builds_participant_request(monkeypatch):
    _configure(monkeypatch)
    from app.config.settings import settings
    from app.services.livekit import sip_service

    monkeypatch.setattr(settings, "livekit_sip_outbound_trunk_id", "ST_abc123")
    fake = _mock_lk_api()
    monkeypatch.setattr(sip_service, "livekit_api_client", lambda: fake)

    await sip_service.dial_outbound_sip(
        room_name="pstn-out-xyz",
        phone_number="+15551234567",
        caller_id="+15559998888",
        participant_identity="pstn-callee",
        participant_name="Callee",
    )

    fake.sip.create_sip_participant.assert_awaited_once()
    req = fake.sip.create_sip_participant.await_args.args[0]
    assert req.sip_trunk_id == "ST_abc123"
    assert req.sip_call_to == "+15551234567"
    assert req.room_name == "pstn-out-xyz"
    assert req.participant_identity == "pstn-callee"
    assert req.participant_name == "Callee"
    assert req.sip_number == "+15559998888"
    assert req.play_dialtone is False


@pytest.mark.asyncio
async def test_hangup_room_calls_delete(monkeypatch):
    _configure(monkeypatch)
    from app.services.livekit import sip_service

    fake = _mock_lk_api()
    monkeypatch.setattr(sip_service, "livekit_api_client", lambda: fake)

    await sip_service.hangup_room("pstn-out-xyz")

    fake.room.delete_room.assert_awaited_once()
    req = fake.room.delete_room.await_args.args[0]
    assert req.room == "pstn-out-xyz"
    fake.aclose.assert_awaited_once()


# ── transfer_twilio_call_to_number ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_transfer_returns_false_on_empty_args():
    from app.services.livekit.sip_service import transfer_twilio_call_to_number
    assert await transfer_twilio_call_to_number(call_sid="", target_number="+12025550143", owner_user_id="u") is False
    assert await transfer_twilio_call_to_number(call_sid="CAabc", target_number="", owner_user_id="u") is False


@pytest.mark.asyncio
async def test_transfer_redirects_via_twilio(monkeypatch):
    """With creds resolvable + Twilio Client mocked, calls.update() is invoked
    with TwiML containing <Dial>, the target number and an action callback."""
    from app.config.settings import settings
    monkeypatch.setattr(settings, "api_base_url", "https://api.convis.test")
    from app.services.livekit import sip_service

    # provider_connections lookup → a doc; decrypt → creds.
    fake_db = MagicMock()
    fake_db.__getitem__.return_value.find_one.return_value = {"_id": "x", "account_sid": "enc", "auth_token": "enc"}
    monkeypatch.setattr("app.config.database.Database.get_db", lambda: fake_db)
    monkeypatch.setattr("app.utils.twilio_helpers.decrypt_twilio_credentials", lambda conn: ("ACtest", "tok"))

    captured = {}
    class _FakeCalls:
        def __init__(self, sid): captured["call_sid"] = sid
        def update(self, twiml=None): captured["twiml"] = twiml
    class _FakeClient:
        def __init__(self, *a): pass
        def calls(self, sid): return _FakeCalls(sid)
    monkeypatch.setattr("twilio.rest.Client", _FakeClient)

    ok = await sip_service.transfer_twilio_call_to_number(
        call_sid="CAabc123", target_number="+12025550143", owner_user_id="user1", direction="inbound",
    )
    assert ok is True
    assert captured["call_sid"] == "CAabc123"
    assert "<Dial" in captured["twiml"]
    assert "+12025550143" in captured["twiml"]
    assert 'answerOnBridge="true"' in captured["twiml"]
    assert "/api/twilio-webhooks/transfer-result?dir=inbound" in captured["twiml"]


@pytest.mark.asyncio
async def test_transfer_no_action_attr_when_api_base_url_unset(monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "api_base_url", None)
    monkeypatch.setattr(settings, "base_url", None)
    from app.services.livekit import sip_service

    fake_db = MagicMock()
    fake_db.__getitem__.return_value.find_one.return_value = None  # no conn
    monkeypatch.setattr("app.config.database.Database.get_db", lambda: fake_db)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACenv")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tokenv")

    captured = {}
    class _FakeCalls:
        def __init__(self, sid): pass
        def update(self, twiml=None): captured["twiml"] = twiml
    class _FakeClient:
        def __init__(self, *a): pass
        def calls(self, sid): return _FakeCalls(sid)
    monkeypatch.setattr("twilio.rest.Client", _FakeClient)

    ok = await sip_service.transfer_twilio_call_to_number(
        call_sid="CAxyz", target_number="+441234567890", owner_user_id=None,
    )
    assert ok is True
    assert "action=" not in captured["twiml"]


@pytest.mark.asyncio
async def test_transfer_returns_false_when_no_creds(monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "api_base_url", "https://api.convis.test")
    from app.services.livekit import sip_service

    fake_db = MagicMock()
    fake_db.__getitem__.return_value.find_one.return_value = None
    monkeypatch.setattr("app.config.database.Database.get_db", lambda: fake_db)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)

    ok = await sip_service.transfer_twilio_call_to_number(
        call_sid="CAabc", target_number="+12025550143", owner_user_id="u",
    )
    assert ok is False
