"""
Verify the inbound TwiML actually requests recording — guards the "every
inbound call is recorded" promise the user demanded.

We disable signature verification (TWILIO_VERIFY_WEBHOOKS=0 in conftest) and
post a Twilio-shaped form to /api/inbound-calls/connect/{assistant_id}, then
assert the TwiML attributes that make Twilio actually record.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET


def _parse_twiml(text: str) -> ET.Element:
    return ET.fromstring(text)


def test_twiml_emits_record_attribute(client, patched_db, make_user, make_assistant, monkeypatch):
    monkeypatch.setenv("LIVEKIT_SIP_INBOUND_HOST", "test-trunk.sip.livekit.cloud")
    monkeypatch.setenv("API_BASE_URL", "https://api.convis.test")

    uid = make_user()
    aid = make_assistant(user_id=uid)

    # Stub _provision_call so we don't touch real LiveKit.
    from app.routes.inbound_calls import inbound_calls as ic_module

    async def _fake_provision(assistant_id, *, direction, from_number=None, **_kw):
        return f"pstn-in-FAKE-{assistant_id}"

    monkeypatch.setattr(ic_module, "_provision_call", _fake_provision)

    resp = client.post(
        f"/api/inbound-calls/connect/{aid}",
        data={"CallSid": "CA_test", "From": "+15551112222", "To": "+15553334444"},
    )
    assert resp.status_code == 200, resp.text

    root = _parse_twiml(resp.text)
    assert root.tag == "Response", f"Expected <Response>, got <{root.tag}>"
    dial = root.find("Dial")
    assert dial is not None, "TwiML must contain a <Dial> element"

    # The patch I shipped should always set record="record-from-answer-dual"
    record = dial.attrib.get("record")
    assert record == "record-from-answer-dual", (
        f"Expected dual-channel recording, got record={record!r}. "
        f"Without this, Twilio captures NO audio for inbound calls — "
        f"directly violates the 'every call recorded' guarantee."
    )

    # Recording callback should be set when API_BASE_URL is configured
    cb = dial.attrib.get("recordingStatusCallback")
    assert cb and "/api/twilio-webhooks/recording" in cb, (
        f"recordingStatusCallback must point to the recording webhook, got {cb!r}"
    )
    method = dial.attrib.get("recordingStatusCallbackMethod")
    assert method == "POST", f"Expected POST, got {method!r}"

    events = dial.attrib.get("recordingStatusCallbackEvent", "")
    assert "completed" in events, f"Must subscribe to 'completed', got {events!r}"
    # We added 'failed' so silent record failures aren't invisible
    assert "failed" in events, (
        f"Must subscribe to 'failed' too — without it, silent recording "
        f"failures are invisible. Got: {events!r}"
    )


def test_twiml_records_even_when_callback_missing(client, patched_db, make_user, make_assistant, monkeypatch):
    """If api_base_url/base_url is unset, we still record — fail-loud, not silent.
    Audio is recoverable from Twilio Console even without our callback URL."""
    monkeypatch.setenv("LIVEKIT_SIP_INBOUND_HOST", "test-trunk.sip.livekit.cloud")
    monkeypatch.delenv("API_BASE_URL", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    # Force settings to reload — settings is a singleton cached at import time
    from app.config import settings as settings_mod
    monkeypatch.setattr(settings_mod.settings, "api_base_url", None)
    monkeypatch.setattr(settings_mod.settings, "base_url", None)

    uid = make_user()
    aid = make_assistant(user_id=uid)

    from app.routes.inbound_calls import inbound_calls as ic_module

    async def _fake_provision(assistant_id, *, direction, from_number=None, **_kw):
        return f"pstn-in-FAKE"
    monkeypatch.setattr(ic_module, "_provision_call", _fake_provision)

    resp = client.post(
        f"/api/inbound-calls/connect/{aid}",
        data={"CallSid": "CA_test_no_cb", "From": "+15551112222", "To": "+15553334444"},
    )
    assert resp.status_code == 200, resp.text

    root = _parse_twiml(resp.text)
    dial = root.find("Dial")
    assert dial is not None
    assert dial.attrib.get("record") == "record-from-answer-dual", (
        "Even without callback URL, recording must still be requested. "
        f"Got: {dict(dial.attrib)}"
    )


def test_call_log_inserted_with_user_id_and_assistant_name(
    client, patched_db, make_user, make_assistant, monkeypatch,
):
    """Regression test for the recently-fixed dashboard-visibility bug."""
    monkeypatch.setenv("LIVEKIT_SIP_INBOUND_HOST", "test-trunk.sip.livekit.cloud")

    uid = make_user(email="owner@convis.test")
    aid = make_assistant(user_id=uid, name="My Real Bot")

    from app.routes.inbound_calls import inbound_calls as ic_module
    async def _fake(*a, **k): return "pstn-in-OK"
    monkeypatch.setattr(ic_module, "_provision_call", _fake)

    client.post(
        f"/api/inbound-calls/connect/{aid}",
        data={"CallSid": "CA_log_check", "From": "+1", "To": "+2"},
    )

    log = patched_db["call_logs"].find_one({"call_sid": "CA_log_check"})
    assert log is not None, "Inbound call_log was not created"
    assert log.get("user_id") == uid, (
        f"call_log.user_id must equal the assistant's owner. Got {log.get('user_id')} expected {uid}. "
        f"Without this, dashboard shows nothing."
    )
    assert log.get("assistant_name") == "My Real Bot", (
        f"call_log.assistant_name must be the actual assistant name. "
        f"Got {log.get('assistant_name')!r}. Empty value renders as 'Unknown Assistant' in UI."
    )
    assert log.get("direction") == "inbound"
