"""Integration tests for the LiveKit-routed endpoints.

We assert end-to-end behaviour: the right LiveKit calls happen, the right TwiML
is returned, DB writes land where expected. LiveKit SDK is stubbed (via
conftest.py) and Mongo is replaced with an in-memory FakeDB fixture.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId


# ─── Helpers ────────────────────────────────────────────────────────────────


def _fake_assistant(**overrides):
    doc = {
        "_id": ObjectId(),
        "user_id": ObjectId(),
        "name": "Concierge",
        "system_message": "You are a concierge.",
        "call_greeting": "Hi!",
        "voice": "alloy",
        "tts_voice": "v1",
        "tts_model": "eleven_flash_v2_5",
        "asr_model": "nova-2",
        "llm_model": "gpt-4o-mini",
        "temperature": 0.5,
    }
    doc.update(overrides)
    return doc


def _configure_livekit(monkeypatch):
    from app.config.settings import settings
    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_api_key", "APItest")
    monkeypatch.setattr(settings, "livekit_api_secret", "secret")
    monkeypatch.setattr(settings, "livekit_agent_name", "convis-agent")
    monkeypatch.setattr(settings, "livekit_sip_inbound_host", "test-sip.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_sip_outbound_trunk_id", "ST_abc")


class _FormReq:
    def __init__(self, method: str, data: dict):
        self.method = method
        self._data = data

    async def form(self):
        return self._data

    @property
    def query_params(self):
        return self._data


# ─── /api/livekit/token ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_browser_token_creates_room_and_mints_jwt(monkeypatch, fake_db):
    _configure_livekit(monkeypatch)
    from app.routes.livekit import routes

    assistant = _fake_assistant()
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["calendar_accounts"].find_one.return_value = None

    created_rooms = []

    async def fake_create_room(*, room_name, assistant_config, metadata_extra=None):
        created_rooms.append((room_name, assistant_config, metadata_extra))
        return room_name

    monkeypatch.setattr(routes, "create_room_with_agent", fake_create_room)

    current_user = {"_id": assistant["user_id"], "user_id": str(assistant["user_id"])}

    with patch("app.config.database.Database.get_db", return_value=fake_db):
        response = await routes.issue_browser_token(
            routes.TokenRequest(assistant_id=str(assistant["_id"])),
            current_user=current_user,
        )

    assert response.livekit_url == "wss://test.livekit.cloud"
    assert response.room_name.startswith("web-")
    assert response.identity == f"user-{assistant['user_id']}"
    assert response.token, "JWT must be returned"

    assert created_rooms, "create_room_with_agent was not called"
    room_name, cfg, extra = created_rooms[0]
    assert room_name == response.room_name
    assert cfg["assistant_id"] == str(assistant["_id"])
    assert extra == {"source": "web", "caller_user_id": str(assistant["user_id"])}


@pytest.mark.asyncio
async def test_issue_browser_token_404_for_missing_assistant(monkeypatch, fake_db):
    _configure_livekit(monkeypatch)
    from fastapi import HTTPException

    from app.routes.livekit import routes

    fake_db["assistants"].find_one.return_value = None

    current_user = {"_id": ObjectId(), "user_id": str(ObjectId())}

    with patch("app.config.database.Database.get_db", return_value=fake_db):
        with pytest.raises(HTTPException) as exc:
            await routes.issue_browser_token(
                routes.TokenRequest(assistant_id=str(ObjectId())),
                current_user=current_user,
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_issue_browser_token_rejects_other_users_assistant(monkeypatch, fake_db):
    """Auth bypass prevention: user A cannot create a room for user B's assistant."""
    _configure_livekit(monkeypatch)
    from fastapi import HTTPException

    from app.routes.livekit import routes

    owner_id = ObjectId()
    attacker_id = ObjectId()
    assistant = _fake_assistant(user_id=owner_id)
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["calendar_accounts"].find_one.return_value = None

    async def fake_create_room(**kwargs):
        raise AssertionError("Should not reach room creation for unauthorized caller")

    monkeypatch.setattr(routes, "create_room_with_agent", fake_create_room)

    attacker = {"_id": attacker_id, "user_id": str(attacker_id)}

    with patch("app.config.database.Database.get_db", return_value=fake_db):
        with pytest.raises(HTTPException) as exc:
            await routes.issue_browser_token(
                routes.TokenRequest(assistant_id=str(assistant["_id"])),
                current_user=attacker,
            )
    # Returns 404 (not 403) to avoid leaking existence of other users' assistants
    assert exc.value.status_code == 404


# ─── /api/inbound-calls/connect/{assistant_id} ──────────────────────────────


@pytest.mark.asyncio
async def test_inbound_webhook_returns_sip_twiml(monkeypatch, fake_db):
    _configure_livekit(monkeypatch)
    from app.routes.inbound_calls import inbound_calls as mod

    assistant = _fake_assistant()
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["calendar_accounts"].find_one.return_value = None
    fake_db["call_logs"].insert_one = MagicMock()

    captured = []

    async def fake_create_room(*, room_name, assistant_config, metadata_extra=None):
        captured.append((room_name, metadata_extra))
        return room_name

    monkeypatch.setattr(mod, "create_room_with_agent", fake_create_room)

    req = _FormReq("POST", {
        "CallSid": "CA123",
        "From": "+15551110000",
        "To": "+15552220000",
    })

    with patch("app.config.database.Database.get_db", return_value=fake_db):
        response = await mod.twilio_inbound_webhook(str(assistant["_id"]), req)

    body = bytes(response.body).decode()
    assert "<Dial" in body
    assert "<Sip>" in body
    assert "test-sip.livekit.cloud" in body
    # Dispatch reports inbound direction + from_number
    assert captured
    _room, extra = captured[0]
    assert extra["direction"] == "inbound"
    assert extra["from_number"] == "+15551110000"

    fake_db["call_logs"].insert_one.assert_called_once()
    log_doc = fake_db["call_logs"].insert_one.call_args.args[0]
    assert log_doc["direction"] == "inbound"
    assert log_doc["call_sid"] == "CA123"
    assert log_doc["livekit_room"].startswith("pstn-in-")


@pytest.mark.asyncio
async def test_inbound_webhook_says_unavailable_when_livekit_unset(monkeypatch, fake_db):
    from app.config.settings import settings
    from app.routes.inbound_calls import inbound_calls as mod

    # Clear the inbound host — everything else configured so we reach the
    # _livekit_sip_uri() call that raises LiveKitNotConfigured.
    monkeypatch.setattr(settings, "livekit_sip_inbound_host", None)
    monkeypatch.setattr(settings, "livekit_url", "wss://x")
    monkeypatch.setattr(settings, "livekit_api_key", "k")
    monkeypatch.setattr(settings, "livekit_api_secret", "s")
    monkeypatch.setattr(settings, "livekit_agent_name", "convis-agent")

    assistant = _fake_assistant()
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["calendar_accounts"].find_one.return_value = None

    async def fake_create_room(*, room_name, assistant_config, metadata_extra=None):
        return room_name

    monkeypatch.setattr(mod, "create_room_with_agent", fake_create_room)

    req = _FormReq("POST", {"CallSid": "CA9", "From": "+1", "To": "+2"})
    with patch("app.config.database.Database.get_db", return_value=fake_db):
        resp = await mod.twilio_inbound_webhook(str(assistant["_id"]), req)

    body = bytes(resp.body).decode()
    assert "<Say>" in body and "unavailable" in body.lower()


# ─── /api/outbound-calls/make-call/{assistant_id} ──────────────────────────


def _current_user(user_id_obj):
    return {
        "_id": user_id_obj,
        "user_id": str(user_id_obj),
        "token_role": "user",
    }


@pytest.mark.asyncio
async def test_make_outbound_call_dispatches_agent_and_dials_sip(monkeypatch, fake_db):
    _configure_livekit(monkeypatch)
    from app.models.outbound_calls import OutboundCallRequest
    from app.routes.outbound_calls import outbound_calls as mod

    assistant = _fake_assistant()
    phone_doc = {
        "_id": ObjectId(),
        "phone_number": "+15559998888",
        "user_id": assistant["user_id"],
        "assigned_assistant_id": assistant["_id"],
        "status": "active",
    }
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["phone_numbers"].find_one.return_value = phone_doc
    fake_db["calendar_accounts"].find_one.return_value = None
    inserted_log = MagicMock()
    inserted_log.inserted_id = ObjectId()
    fake_db["call_logs"].insert_one = MagicMock(return_value=inserted_log)
    fake_db["call_logs"].update_one = MagicMock()

    created = []
    dialed = []

    async def fake_create_room(*, room_name, assistant_config, metadata_extra=None):
        created.append(room_name)
        return room_name

    async def fake_dial(*, room_name, phone_number, caller_id=None, **kwargs):
        dialed.append((room_name, phone_number, caller_id))
        return "pstn-callee"

    monkeypatch.setattr(mod, "create_room_with_agent", fake_create_room)
    monkeypatch.setattr(mod, "dial_outbound_sip", fake_dial)

    fake_request = MagicMock()
    with patch("app.config.database.Database.get_db", return_value=fake_db):
        response = await mod.make_outbound_call(
            request=fake_request,
            assistant_id=str(assistant["_id"]),
            body=OutboundCallRequest(phone_number="+15551234567"),
            current_user=_current_user(assistant["user_id"]),
        )

    assert response.status == "initiated"
    assert response.call_sid.startswith("pstn-out-")
    assert created and created[0] == response.call_sid
    assert dialed and dialed[0][1] == "+15551234567"
    assert dialed[0][2] == "+15559998888"

    log = fake_db["call_logs"].insert_one.call_args.args[0]
    assert log["direction"] == "outbound"
    # Initial state is "initiating" — only flips to "initiated" on success
    assert log["status"] == "initiating"
    assert log["voice_config"]["transport"] == "livekit-sip"
    # The follow-up update fills in final fields
    update_call = fake_db["call_logs"].update_one.call_args
    assert update_call.args[1]["$set"]["status"] == "initiated"


@pytest.mark.asyncio
async def test_make_outbound_rejects_other_users_assistant(monkeypatch, fake_db):
    """SECURITY: caller cannot place outbound on someone else's assistant."""
    _configure_livekit(monkeypatch)
    from app.models.outbound_calls import OutboundCallRequest
    from app.routes.outbound_calls import outbound_calls as mod
    from fastapi import HTTPException

    owner_id = ObjectId()
    attacker_id = ObjectId()
    assistant = _fake_assistant(user_id=owner_id)

    # The compound (assistant_id + user_id) lookup must return None for the
    # attacker — fake_db simulates that by short-circuiting on user_id mismatch.
    fake_db["assistants"].find_one.return_value = None

    async def boom(*a, **k):
        raise AssertionError("dial path must NOT run for non-owner")

    monkeypatch.setattr(mod, "create_room_with_agent", boom)
    monkeypatch.setattr(mod, "dial_outbound_sip", boom)
    monkeypatch.setattr(mod, "dial_outbound_via_twilio", boom)

    fake_request = MagicMock()
    with patch("app.config.database.Database.get_db", return_value=fake_db):
        with pytest.raises(HTTPException) as exc:
            await mod.make_outbound_call(
                request=fake_request,
                assistant_id=str(assistant["_id"]),
                body=OutboundCallRequest(phone_number="+15551234567"),
                current_user=_current_user(attacker_id),
            )
    # 404 (not 403) so existence isn't leaked
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_make_outbound_ignores_client_user_id_uses_jwt(monkeypatch, fake_db):
    """SECURITY: even if the JSON body somehow had a user_id, only the JWT
    user_id is used to look up the assistant + phone number. Mass-assignment
    of ownership is blocked."""
    _configure_livekit(monkeypatch)
    from app.models.outbound_calls import OutboundCallRequest
    from app.routes.outbound_calls import outbound_calls as mod

    jwt_user = ObjectId()
    other_user = ObjectId()
    assistant = _fake_assistant(user_id=jwt_user)
    phone_doc = {
        "_id": ObjectId(),
        "phone_number": "+15559998888",
        "user_id": jwt_user,
        "assigned_assistant_id": assistant["_id"],
        "status": "active",
    }
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["phone_numbers"].find_one.return_value = phone_doc
    fake_db["calendar_accounts"].find_one.return_value = None
    inserted_log = MagicMock()
    inserted_log.inserted_id = ObjectId()
    fake_db["call_logs"].insert_one = MagicMock(return_value=inserted_log)
    fake_db["call_logs"].update_one = MagicMock()

    async def fake_create_room(*, room_name, assistant_config, metadata_extra=None):
        return room_name

    async def fake_dial(**kwargs):
        return "pstn-callee"

    monkeypatch.setattr(mod, "create_room_with_agent", fake_create_room)
    monkeypatch.setattr(mod, "dial_outbound_sip", fake_dial)

    fake_request = MagicMock()
    with patch("app.config.database.Database.get_db", return_value=fake_db):
        response = await mod.make_outbound_call(
            request=fake_request,
            assistant_id=str(assistant["_id"]),
            body=OutboundCallRequest(phone_number="+15551234567"),
            current_user=_current_user(jwt_user),
        )

    assert response.status == "initiated"
    # The OWNERSHIP-check lookup (the FIRST find_one on assistants) MUST have
    # filtered by the JWT user_id. Subsequent calls (e.g. load_assistant_config)
    # don't carry the user_id filter — they happen after auth has been verified.
    first_assistant_query = fake_db["assistants"].find_one.call_args_list[0].args[0]
    assert first_assistant_query["user_id"] == jwt_user


@pytest.mark.asyncio
async def test_make_outbound_falls_back_to_twilio_twiml_when_no_sip_trunk(monkeypatch, fake_db):
    """Path A: when LIVEKIT_SIP_OUTBOUND_TRUNK_ID is empty, route through Twilio
    Programmable Voice with a TwiML <Sip> bridge to the LiveKit room. Required
    for users who don't want to provision an Elastic SIP Trunk."""
    from app.config.settings import settings
    from app.models.outbound_calls import OutboundCallRequest
    from app.routes.outbound_calls import outbound_calls as mod

    # LiveKit + Twilio configured, but NO outbound SIP trunk → TwiML path.
    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_api_key", "k")
    monkeypatch.setattr(settings, "livekit_api_secret", "s")
    monkeypatch.setattr(settings, "livekit_agent_name", "convis-agent")
    monkeypatch.setattr(settings, "livekit_sip_inbound_host", "test-sip.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_sip_outbound_trunk_id", None)
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "authtest")
    monkeypatch.setattr(settings, "api_base_url", "https://api.example.com")

    assistant = _fake_assistant()
    phone_doc = {
        "_id": ObjectId(),
        "phone_number": "+15559998888",
        "user_id": assistant["user_id"],
        "assigned_assistant_id": assistant["_id"],
        "status": "active",
    }
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["phone_numbers"].find_one.return_value = phone_doc
    fake_db["calendar_accounts"].find_one.return_value = None
    inserted_log = MagicMock()
    inserted_log.inserted_id = ObjectId()
    fake_db["call_logs"].insert_one = MagicMock(return_value=inserted_log)
    fake_db["call_logs"].update_one = MagicMock()

    async def fake_create_room(*, room_name, assistant_config, metadata_extra=None):
        return room_name

    def boom_sip(**kwargs):
        raise AssertionError("dial_outbound_sip must NOT be called when no SIP trunk")

    twilio_calls = []

    async def fake_twilio_dial(*, room_name, phone_number, caller_id, status_callback_url=None):
        twilio_calls.append({
            "room_name": room_name,
            "phone_number": phone_number,
            "caller_id": caller_id,
            "status_callback_url": status_callback_url,
        })
        return "CAfaketwiliosid12345"

    monkeypatch.setattr(mod, "create_room_with_agent", fake_create_room)
    monkeypatch.setattr(mod, "dial_outbound_sip", boom_sip)
    monkeypatch.setattr(mod, "dial_outbound_via_twilio", fake_twilio_dial)

    fake_request = MagicMock()
    with patch("app.config.database.Database.get_db", return_value=fake_db):
        response = await mod.make_outbound_call(
            request=fake_request,
            assistant_id=str(assistant["_id"]),
            body=OutboundCallRequest(phone_number="+15551234567"),
            current_user=_current_user(assistant["user_id"]),
        )

    # Twilio leg was placed exactly once with the right destination + caller-id
    assert len(twilio_calls) == 1
    call = twilio_calls[0]
    assert call["phone_number"] == "+15551234567"
    assert call["caller_id"] == "+15559998888"
    assert call["room_name"].startswith("pstn-out-")
    assert call["status_callback_url"] == "https://api.example.com/webhooks/twilio/calls"

    # call_sid is the Twilio SID (so status callbacks match), not the room
    assert response.call_sid == "CAfaketwiliosid12345"

    # Initial log + follow-up update
    log = fake_db["call_logs"].insert_one.call_args.args[0]
    assert log["livekit_room"].startswith("pstn-out-")
    assert log["voice_config"]["transport"] == "twilio-twiml"
    update_call = fake_db["call_logs"].update_one.call_args
    update_set = update_call.args[1]["$set"]
    assert update_set["call_sid"] == "CAfaketwiliosid12345"
    assert update_set["twilio_call_sid"] == "CAfaketwiliosid12345"
    assert update_set["status"] == "initiated"


@pytest.mark.asyncio
async def test_twiml_path_503_when_twilio_creds_missing(monkeypatch, fake_db):
    """If neither SIP trunk NOR Twilio creds are set, dialing must 503 — not
    silently succeed."""
    from fastapi import HTTPException

    from app.config.settings import settings
    from app.models.outbound_calls import OutboundCallRequest
    from app.routes.outbound_calls import outbound_calls as mod

    monkeypatch.setattr(settings, "livekit_url", "wss://x")
    monkeypatch.setattr(settings, "livekit_api_key", "k")
    monkeypatch.setattr(settings, "livekit_api_secret", "s")
    monkeypatch.setattr(settings, "livekit_agent_name", "convis-agent")
    monkeypatch.setattr(settings, "livekit_sip_inbound_host", "test-sip.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_sip_outbound_trunk_id", None)
    monkeypatch.setattr(settings, "twilio_account_sid", None)
    monkeypatch.setattr(settings, "twilio_auth_token", None)

    assistant = _fake_assistant()
    phone_doc = {
        "_id": ObjectId(),
        "phone_number": "+15559998888",
        "user_id": assistant["user_id"],
        "assigned_assistant_id": assistant["_id"],
        "status": "active",
    }
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["phone_numbers"].find_one.return_value = phone_doc
    fake_db["calendar_accounts"].find_one.return_value = None
    inserted_log = MagicMock()
    inserted_log.inserted_id = ObjectId()
    fake_db["call_logs"].insert_one = MagicMock(return_value=inserted_log)
    fake_db["call_logs"].update_one = MagicMock()

    async def fake_create_room(*, room_name, assistant_config, metadata_extra=None):
        return room_name

    async def fake_hangup_room(room_name):
        return None

    monkeypatch.setattr(mod, "create_room_with_agent", fake_create_room)
    monkeypatch.setattr(mod, "hangup_room", fake_hangup_room)

    fake_request = MagicMock()
    with patch("app.config.database.Database.get_db", return_value=fake_db):
        with pytest.raises(HTTPException) as exc:
            await mod.make_outbound_call(
                request=fake_request,
                assistant_id=str(assistant["_id"]),
                body=OutboundCallRequest(phone_number="+15551234567"),
                current_user=_current_user(assistant["user_id"]),
            )
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_twiml_bridge_targets_livekit_inbound_host():
    """The TwiML returned to Twilio must <Sip>sip:<room>@<LIVEKIT_SIP_INBOUND_HOST></Sip>.

    Wrong host = call goes nowhere. Bug-guard for accidentally hardcoding a host.
    """
    from app.config.settings import settings
    from app.services.twilio_outbound import build_bridge_twiml

    settings.livekit_sip_inbound_host = "convis-9n03cws2-sip.livekit.cloud"
    twiml = build_bridge_twiml("pstn-out-abc123")
    assert "<Dial" in twiml
    assert "<Sip>" in twiml
    assert "sip:pstn-out-abc123@convis-9n03cws2-sip.livekit.cloud" in twiml
    # answer-on-bridge avoids early Twilio audio leaking before LiveKit is ready
    assert 'answerOnBridge="true"' in twiml or "answer_on_bridge" in twiml.lower()


@pytest.mark.asyncio
async def test_make_outbound_rejects_non_e164(monkeypatch):
    _configure_livekit(monkeypatch)
    from fastapi import HTTPException

    from app.models.outbound_calls import OutboundCallRequest
    from app.routes.outbound_calls import outbound_calls as mod

    fake_request = MagicMock()
    # E.164 check happens before any DB call, so no db patch needed.
    with pytest.raises(HTTPException) as exc:
        await mod.make_outbound_call(
            request=fake_request,
            assistant_id=str(ObjectId()),
            body=OutboundCallRequest(phone_number="bad-number"),
            current_user=_current_user(ObjectId()),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_make_outbound_requires_assigned_phone_number(monkeypatch, fake_db):
    _configure_livekit(monkeypatch)
    from fastapi import HTTPException

    from app.models.outbound_calls import OutboundCallRequest
    from app.routes.outbound_calls import outbound_calls as mod

    assistant = _fake_assistant()
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["phone_numbers"].find_one.return_value = None

    fake_request = MagicMock()
    with patch("app.config.database.Database.get_db", return_value=fake_db):
        with pytest.raises(HTTPException) as exc:
            await mod.make_outbound_call(
                request=fake_request,
                assistant_id=str(assistant["_id"]),
                body=OutboundCallRequest(phone_number="+15551234567"),
                current_user=_current_user(assistant["user_id"]),
            )
    assert exc.value.status_code == 400
    assert "phone number" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_hangup_requires_owner(monkeypatch, fake_db):
    _configure_livekit(monkeypatch)
    from app.routes.outbound_calls import outbound_calls as mod

    user_id = ObjectId()
    call_sid = "pstn-out-xyz"
    fake_db["call_logs"].find_one.return_value = {
        "_id": ObjectId(),
        "call_sid": call_sid,
        "user_id": user_id,
        "livekit_room": call_sid,
    }
    fake_db["call_logs"].update_one = MagicMock()

    hung = []

    async def fake_hangup(room_name):
        hung.append(room_name)

    monkeypatch.setattr(mod, "hangup_room", fake_hangup)

    fake_request = MagicMock()
    with patch("app.config.database.Database.get_db", return_value=fake_db):
        result = await mod.hangup_call(
            request=fake_request,
            call_sid=call_sid,
            current_user=_current_user(user_id),
        )
    assert result["status"] == "completed"
    assert hung == [call_sid]


@pytest.mark.asyncio
async def test_hangup_404_for_other_users_call(monkeypatch, fake_db):
    """SECURITY: a non-owner cannot hang up another user's call. The DB
    lookup is filtered by (call_sid, user_id) so the find_one returns None."""
    _configure_livekit(monkeypatch)
    from fastapi import HTTPException

    from app.routes.outbound_calls import outbound_calls as mod

    fake_db["call_logs"].find_one.return_value = None  # filter mismatch

    async def boom(room_name):
        raise AssertionError("hangup_room must NOT be called for non-owner")

    monkeypatch.setattr(mod, "hangup_room", boom)

    fake_request = MagicMock()
    with patch("app.config.database.Database.get_db", return_value=fake_db):
        with pytest.raises(HTTPException) as exc:
            await mod.hangup_call(
                request=fake_request,
                call_sid="pstn-out-xyz",
                current_user=_current_user(ObjectId()),
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_call_status_404_for_other_users_call(monkeypatch, fake_db):
    """SECURITY: get_call_status must not leak PII (from/to numbers, duration)
    to anyone but the call's owner."""
    from fastapi import HTTPException

    from app.routes.outbound_calls import outbound_calls as mod

    fake_db["call_logs"].find_one.return_value = None  # filter mismatch

    fake_request = MagicMock()
    with patch("app.config.database.Database.get_db", return_value=fake_db):
        with pytest.raises(HTTPException) as exc:
            await mod.get_call_status(
                request=fake_request,
                call_sid="pstn-out-xyz",
                current_user=_current_user(ObjectId()),
            )
    assert exc.value.status_code == 404
