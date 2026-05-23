"""Tests for the call-transfer-to-human feature.

Covers:
  - The /api/twilio-webhooks/transfer-result webhook (Twilio <Dial action>
    callback): completed → <Hangup/>; no-answer → re-bridge to a fresh agent.
  - The AIAssistantCreate / AIAssistantUpdate Pydantic validators (E.164 number
    required when call_transfer_enabled).
"""
from __future__ import annotations

from datetime import datetime
from xml.etree import ElementTree as ET

import pytest
from bson import ObjectId


def _twiml(text: str) -> ET.Element:
    return ET.fromstring(text)


# ── /api/twilio-webhooks/transfer-result ────────────────────────────────────

def test_transfer_result_completed_returns_hangup(client, patched_db):
    """If the human answered and the call ended normally, just hang up."""
    resp = client.post(
        "/api/twilio-webhooks/transfer-result",
        data={"CallSid": "CAabc", "DialCallStatus": "completed"},
    )
    assert resp.status_code == 200, resp.text
    root = _twiml(resp.text)
    assert root.tag == "Response"
    assert root.find("Hangup") is not None
    # No <Dial> on a completed transfer.
    assert root.find("Dial") is None


def test_transfer_result_no_answer_rebridges_to_agent(client, patched_db, make_user, make_assistant, monkeypatch):
    """No-answer / busy / failed → re-provision a fresh LiveKit room with the
    same assistant + resumed_after_failed_transfer, and re-bridge the caller."""
    monkeypatch.setenv("LIVEKIT_SIP_INBOUND_HOST", "test-trunk.sip.livekit.cloud")
    from app.config import settings as settings_mod
    monkeypatch.setattr(settings_mod.settings, "livekit_sip_inbound_host", "test-trunk.sip.livekit.cloud")

    uid = make_user()
    aid = make_assistant(user_id=uid, name="Front Desk")
    # The call_log Twilio is reporting on (from a prior transfer attempt).
    patched_db["call_logs"].insert_one({
        "call_sid": "CAxfer1",
        "assistant_id": aid,
        "user_id": uid,
        "direction": "inbound",
        "from_number": "+15551112222",
        "status": "in-progress",
        "transferred": True,
        "transferred_to": "+19998887777",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    })

    captured = {}
    from app.routes.inbound_calls import inbound_calls as ic_module
    async def _fake_provision(assistant_id, *, direction, from_number=None, call_sid=None,
                              resumed_after_failed_transfer=False, **_kw):
        captured["assistant_id"] = assistant_id
        captured["direction"] = direction
        captured["call_sid"] = call_sid
        captured["resumed"] = resumed_after_failed_transfer
        return "pstn-in-RESUMED"
    monkeypatch.setattr(ic_module, "_provision_call", _fake_provision)

    resp = client.post(
        "/api/twilio-webhooks/transfer-result?dir=inbound",
        data={"CallSid": "CAxfer1", "DialCallStatus": "no-answer"},
    )
    assert resp.status_code == 200, resp.text
    root = _twiml(resp.text)
    dial = root.find("Dial")
    assert dial is not None, f"Expected <Dial><Sip> on no-answer, got: {resp.text}"
    sip = dial.find("Sip")
    assert sip is not None and "pstn-in-RESUMED" in sip.text

    # The fake _provision_call got the resume flag + same assistant + call_sid.
    assert captured["assistant_id"] == str(aid)
    assert captured["resumed"] is True
    assert captured["call_sid"] == "CAxfer1"

    # call_log stamped with the failed-transfer outcome + re-pointed room.
    log = patched_db["call_logs"].find_one({"call_sid": "CAxfer1"})
    assert log["transfer_failed"] is True
    assert log["transfer_failure_status"] == "no-answer"
    assert log["livekit_room"] == "pstn-in-RESUMED"


def test_transfer_result_no_assistant_says_sorry(client, patched_db):
    """No matching call_log / assistant → polite <Say> + <Hangup/>, never 500."""
    resp = client.post(
        "/api/twilio-webhooks/transfer-result",
        data={"CallSid": "CAunknown", "DialCallStatus": "busy"},
    )
    assert resp.status_code == 200, resp.text
    root = _twiml(resp.text)
    assert root.find("Hangup") is not None
    assert root.find("Dial") is None


# ── AIAssistantCreate / AIAssistantUpdate validators ────────────────────────

def test_assistant_create_rejects_bad_transfer_number():
    from app.models.ai_assistant import AIAssistantCreate
    with pytest.raises(ValueError, match="E.164"):
        AIAssistantCreate(
            user_id="u1", name="Bot", system_message="hi",
            call_transfer_enabled=True, call_transfer_number="not-a-number",
        )


def test_assistant_create_accepts_valid_transfer_number():
    from app.models.ai_assistant import AIAssistantCreate
    a = AIAssistantCreate(
        user_id="u1", name="Bot", system_message="hi",
        call_transfer_enabled=True, call_transfer_number="+12025550143",
        call_transfer_message="Hold on", call_transfer_conditions="billing",
    )
    assert a.call_transfer_enabled is True
    assert a.call_transfer_number == "+12025550143"


def test_assistant_create_transfer_off_ignores_number():
    """When transfer is off, an empty/missing number is fine — no validation error."""
    from app.models.ai_assistant import AIAssistantCreate
    a = AIAssistantCreate(user_id="u1", name="Bot", system_message="hi")
    assert a.call_transfer_enabled is False
    assert a.call_transfer_number is None


def test_assistant_update_rejects_bad_number_format():
    """A PATCH that supplies a malformed number is rejected by the model
    regardless of the enable flag."""
    from app.models.ai_assistant import AIAssistantUpdate
    with pytest.raises(ValueError, match="E.164"):
        AIAssistantUpdate(call_transfer_number="garbage")


def test_assistant_update_enable_without_number_passes_model_layer():
    """A PATCH of just {call_transfer_enabled: true} is fine at the model layer
    (the route guard will reject if the doc has no number either)."""
    from app.models.ai_assistant import AIAssistantUpdate
    u = AIAssistantUpdate(call_transfer_enabled=True)
    assert u.call_transfer_enabled is True


def test_assistant_update_enable_with_number_ok():
    from app.models.ai_assistant import AIAssistantUpdate
    u = AIAssistantUpdate(call_transfer_enabled=True, call_transfer_number="+441234567890")
    assert u.call_transfer_enabled is True
