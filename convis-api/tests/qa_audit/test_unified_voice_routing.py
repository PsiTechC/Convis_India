"""
Adversarial tests for the unified Twilio voice-routing layer.

Two pieces are under test:

1. `app/utils/twilio_voice_routing.py` — the helper every Convis code path
   funnels through to set a Twilio number's `voice_url` / clear its
   `voice_application_sid`. Regressions here re-introduce the
   silent-"Change AI" bug from production.

2. `/api/twilio-webhooks/voice` — the dynamic webhook that ALL inbound calls
   flow through. Pre-fix this endpoint emitted `<Connect><Stream>` to a dead
   WebSocket pipeline; post-fix it emits `<Dial><Sip>` to LiveKit.

Each test is written so it FAILS if the corresponding bug were re-introduced.
The docstring on each test is the bug-report payload that would land in the
QA report if the test ever flips red.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

import pytest
from bson import ObjectId


# ============================================================================
# Helper unit tests — twilio_voice_routing.py
# ============================================================================


def _import_helper(monkeypatch=None, *, api_base_url="https://api.convis.test", base_url=None):
    """Re-import the helper module with a patched settings singleton.
    Settings is module-level cached so monkeypatching attributes on the
    instance is the supported way to flip behaviour in tests."""
    from app.config import settings as settings_mod
    if monkeypatch is not None:
        monkeypatch.setattr(settings_mod.settings, "api_base_url", api_base_url)
        monkeypatch.setattr(settings_mod.settings, "base_url", base_url)
    from app.utils import twilio_voice_routing as mod
    return mod


class TestUnifiedVoiceUrl:
    def test_uses_api_base_url_when_set(self, monkeypatch):
        mod = _import_helper(monkeypatch, api_base_url="https://api.convis.test", base_url=None)
        assert mod.unified_voice_url() == "https://api.convis.test/api/twilio-webhooks/voice"

    def test_strips_trailing_slash(self, monkeypatch):
        mod = _import_helper(monkeypatch, api_base_url="https://api.convis.test/", base_url=None)
        assert mod.unified_voice_url() == "https://api.convis.test/api/twilio-webhooks/voice"

    def test_falls_back_to_base_url(self, monkeypatch):
        mod = _import_helper(monkeypatch, api_base_url=None, base_url="https://fallback.example")
        assert mod.unified_voice_url() == "https://fallback.example/api/twilio-webhooks/voice"

    def test_returns_none_when_both_unset(self, monkeypatch):
        """Without a public URL we have no way to compute the webhook —
        helper MUST return None so callers can fail loud instead of writing
        junk URLs to Twilio."""
        mod = _import_helper(monkeypatch, api_base_url=None, base_url=None)
        assert mod.unified_voice_url() is None
        assert mod.unified_voice_status_url() is None


class TestEnsureUnifiedVoiceRoutingHappyPath:
    """
    The helper is the single hand-off between Convis and Twilio for voice
    routing. Every code path (purchase, import, refresh, assign-assistant,
    backfill) calls it. The contract:

      - voice_url            = unified webhook URL
      - voice_method         = POST
      - voice_application_sid = ""           ← REGRESSION GUARD
      - status_callback      = unified status URL
      - status_callback_method = POST
    """

    def test_calls_twilio_with_correct_kwargs(self, monkeypatch):
        mod = _import_helper(monkeypatch)
        client = MagicMock()
        ok, msg = mod.ensure_unified_voice_routing(client, "PNabc123", label="+15551234567")

        assert ok is True
        assert msg == "https://api.convis.test/api/twilio-webhooks/voice"

        client.incoming_phone_numbers.assert_called_once_with("PNabc123")
        update = client.incoming_phone_numbers.return_value.update
        update.assert_called_once()
        kwargs = update.call_args.kwargs

        assert kwargs["voice_url"] == "https://api.convis.test/api/twilio-webhooks/voice"
        assert kwargs["voice_method"] == "POST"
        assert kwargs["status_callback"] == "https://api.convis.test/api/twilio-webhooks/voice-status"
        assert kwargs["status_callback_method"] == "POST"

    def test_explicitly_clears_voice_application_sid(self, monkeypatch):
        """REGRESSION GUARD: voice_application_sid='' MUST be passed.

        If a TwiML App is attached to the number, Twilio's `voice_url` is
        IGNORED in favour of the App's URL. Numbers purchased through the
        old dashboard flow have App SIDs pointing at a deprecated WSS
        pipeline; without explicitly clearing voice_application_sid every
        update is silently a no-op and "Change AI" / inbound routing
        breaks. Bug previously shipped to production for ~weeks.
        """
        mod = _import_helper(monkeypatch)
        client = MagicMock()
        mod.ensure_unified_voice_routing(client, "PNabc123")

        kwargs = client.incoming_phone_numbers.return_value.update.call_args.kwargs
        assert "voice_application_sid" in kwargs, (
            "voice_application_sid must be in the update kwargs — without it "
            "Twilio leaves any existing TwiML App attached and our voice_url "
            "write is silently overridden."
        )
        assert kwargs["voice_application_sid"] == "", (
            f"voice_application_sid must be empty string to detach the App, "
            f"got {kwargs['voice_application_sid']!r}"
        )


class TestEnsureUnifiedVoiceRoutingFailureModes:
    def test_returns_false_when_api_base_url_unset_and_does_not_call_twilio(self, monkeypatch):
        """If we can't compute the webhook URL we MUST refuse to write —
        otherwise we'd null/empty the customer's voice_url and silently
        break their inbound calls."""
        mod = _import_helper(monkeypatch, api_base_url=None, base_url=None)
        client = MagicMock()
        ok, msg = mod.ensure_unified_voice_routing(client, "PNabc", label="+15551234567")

        assert ok is False
        assert "api_base_url unset" in msg
        client.incoming_phone_numbers.assert_not_called()

    def test_swallows_twilio_exceptions(self, monkeypatch):
        """Caller iterates over many numbers — one Twilio API blip MUST
        NOT abort the loop. Helper returns (False, msg); caller logs and
        continues."""
        mod = _import_helper(monkeypatch)
        client = MagicMock()
        client.incoming_phone_numbers.return_value.update.side_effect = RuntimeError(
            "HTTP 429 Too Many Requests"
        )

        # The helper must not let this exception propagate
        ok, msg = mod.ensure_unified_voice_routing(client, "PNabc", label="+15551234567")

        assert ok is False
        assert "429" in msg or "Too Many Requests" in msg

    def test_unknown_provider_sid_treated_as_failure_not_crash(self, monkeypatch):
        """A wrong/stale SID raises from the Twilio SDK; the helper must
        capture it and return (False, msg) so import loops don't abort."""
        mod = _import_helper(monkeypatch)
        client = MagicMock()
        # twilio.base.exceptions.TwilioRestException would normally raise here.
        # Use a plain Exception — the helper's `except Exception` covers both.
        client.incoming_phone_numbers.return_value.update.side_effect = Exception(
            "HTTP 404: The requested resource ... was not found"
        )

        ok, msg = mod.ensure_unified_voice_routing(client, "PNbogus")
        assert ok is False
        assert "404" in msg or "not found" in msg.lower()


# ============================================================================
# /api/twilio-webhooks/voice — adversarial probes
# ============================================================================


def _twiml(text: str) -> ET.Element:
    return ET.fromstring(text)


@pytest.fixture
def webhook_env(monkeypatch):
    """Set the env the /voice endpoint needs to reach the happy-path branch."""
    monkeypatch.setenv("LIVEKIT_SIP_INBOUND_HOST", "test-trunk.sip.livekit.cloud")
    monkeypatch.setenv("API_BASE_URL", "https://api.convis.test")
    from app.config import settings as settings_mod
    monkeypatch.setattr(
        settings_mod.settings,
        "livekit_sip_inbound_host",
        "test-trunk.sip.livekit.cloud",
    )
    monkeypatch.setattr(settings_mod.settings, "api_base_url", "https://api.convis.test")
    yield


def _stub_provision(monkeypatch, *, room_name="pstn-in-FAKE", raises=None):
    """Patch the /voice endpoint's lazy import target so we never touch real LiveKit."""
    from app.routes.inbound_calls import inbound_calls as ic_module

    async def _fake(assistant_id, *, direction, from_number=None, **_kw):
        if raises is not None:
            raise raises
        return room_name

    monkeypatch.setattr(ic_module, "_provision_call", _fake)


class TestVoiceWebhookMissingInputs:
    def test_missing_to_returns_safe_twiml_no_exception(self, client, patched_db, webhook_env):
        """Adversarial: Twilio retry / malformed POST without a `To`. Endpoint
        MUST NOT 500 — it returns TwiML 'could not process' and Twilio plays
        it to the caller."""
        resp = client.post("/api/twilio-webhooks/voice", data={"From": "+15551112222"})
        assert resp.status_code == 200, resp.text
        assert "could not process" in resp.text.lower()
        # No call_log row is left behind for a malformed request.
        assert patched_db["call_logs"].count_documents({}) == 0

    def test_unknown_To_number_does_not_leak_existence(self, client, patched_db, webhook_env):
        """An attacker dialing a random number must get a generic 'not
        configured' message — NOT enumerate-able via TwiML differences."""
        resp = client.post(
            "/api/twilio-webhooks/voice",
            data={"To": "+19990001111", "From": "+15551112222", "CallSid": "CA_unknown"},
        )
        assert resp.status_code == 200
        assert "not configured" in resp.text.lower()
        # No <Dial> / <Sip> emitted — caller never gets bridged anywhere.
        root = _twiml(resp.text)
        assert root.find("Dial") is None
        assert root.find("Connect") is None


class TestVoiceWebhookConfigErrors:
    def test_phone_without_assigned_assistant(self, client, patched_db, make_user, webhook_env):
        uid = make_user()
        patched_db["phone_numbers"].insert_one({
            "_id": ObjectId(),
            "user_id": uid,
            "phone_number": "+15553334444",
            "provider": "twilio",
            "provider_sid": "PNxyz",
            # NO assigned_assistant_id
        })
        resp = client.post(
            "/api/twilio-webhooks/voice",
            data={"To": "+15553334444", "From": "+15551112222", "CallSid": "CA_unassigned"},
        )
        assert resp.status_code == 200
        assert "not yet configured" in resp.text.lower()
        root = _twiml(resp.text)
        assert root.find("Dial") is None

    def test_phone_pointing_at_dangling_assistant(self, client, patched_db, make_user, webhook_env):
        """A phone_number doc references an assistant that's been deleted —
        endpoint MUST emit a safe message, NOT 500. If it 500'd Twilio would
        play a generic 'application error' tone to the caller."""
        uid = make_user()
        ghost_assistant_id = ObjectId()  # never inserted
        patched_db["phone_numbers"].insert_one({
            "_id": ObjectId(),
            "user_id": uid,
            "phone_number": "+15553334445",
            "provider": "twilio",
            "provider_sid": "PNxyz2",
            "assigned_assistant_id": ghost_assistant_id,
        })
        resp = client.post(
            "/api/twilio-webhooks/voice",
            data={"To": "+15553334445", "From": "+15551112222", "CallSid": "CA_dangling"},
        )
        assert resp.status_code == 200
        assert "configuration error" in resp.text.lower()
        root = _twiml(resp.text)
        assert root.find("Dial") is None


class TestVoiceWebhookLiveKitFailures:
    def test_livekit_host_unset_returns_safe_unavailable(
        self, client, patched_db, make_user, make_assistant, monkeypatch,
    ):
        """If LIVEKIT_SIP_INBOUND_HOST is missing we'd otherwise produce a
        broken `sip:room@` URI and Twilio would fail silently. We refuse
        the call up front."""
        monkeypatch.delenv("LIVEKIT_SIP_INBOUND_HOST", raising=False)
        from app.config import settings as settings_mod
        monkeypatch.setattr(settings_mod.settings, "livekit_sip_inbound_host", None)

        uid = make_user()
        aid = make_assistant(user_id=uid)
        patched_db["phone_numbers"].insert_one({
            "_id": ObjectId(),
            "user_id": uid,
            "phone_number": "+15558889999",
            "provider": "twilio",
            "provider_sid": "PNzzz",
            "assigned_assistant_id": aid,
        })

        resp = client.post(
            "/api/twilio-webhooks/voice",
            data={"To": "+15558889999", "From": "+15551112222", "CallSid": "CA_no_lk_host"},
        )
        assert resp.status_code == 200
        assert "temporarily unavailable" in resp.text.lower()

    def test_livekit_provision_failure_returns_safe_unavailable(
        self, client, patched_db, make_user, make_assistant, monkeypatch, webhook_env,
    ):
        """LiveKitNotConfigured at provision-time (e.g. API keys missing)
        must surface as a polite TwiML message, NOT a 500."""
        from app.services.livekit.tokens import LiveKitNotConfigured
        _stub_provision(monkeypatch, raises=LiveKitNotConfigured("LIVEKIT_API_KEY missing"))

        uid = make_user()
        aid = make_assistant(user_id=uid)
        patched_db["phone_numbers"].insert_one({
            "_id": ObjectId(),
            "user_id": uid,
            "phone_number": "+15558889990",
            "provider": "twilio",
            "provider_sid": "PNyyy",
            "assigned_assistant_id": aid,
        })

        resp = client.post(
            "/api/twilio-webhooks/voice",
            data={"To": "+15558889990", "From": "+15551112222", "CallSid": "CA_lk_fail"},
        )
        assert resp.status_code == 200
        assert "temporarily unavailable" in resp.text.lower()
        root = _twiml(resp.text)
        assert root.find("Dial") is None


class TestVoiceWebhookHappyPath:
    def test_emits_dial_sip_with_recording_and_logs_call(
        self, client, patched_db, make_user, make_assistant, monkeypatch, webhook_env,
    ):
        """Full end-to-end: number → assistant → LiveKit room → SIP TwiML.
        Plus call_log stamped with user_id (dashboard-visibility regression
        guard) and direction='inbound', plus dual-channel recording."""
        _stub_provision(monkeypatch, room_name="pstn-in-HAPPY")

        uid = make_user(email="ops@example.com")
        aid = make_assistant(user_id=uid, name="Front Desk Bot")
        patched_db["phone_numbers"].insert_one({
            "_id": ObjectId(),
            "user_id": uid,
            "phone_number": "+15557776666",
            "provider": "twilio",
            "provider_sid": "PNgood",
            "assigned_assistant_id": aid,
        })

        resp = client.post(
            "/api/twilio-webhooks/voice",
            data={
                "To": "+15557776666",
                "From": "+15551112222",
                "CallSid": "CA_happy",
                "AccountSid": "ACtest",
            },
        )
        assert resp.status_code == 200, resp.text

        root = _twiml(resp.text)
        dial = root.find("Dial")
        assert dial is not None, f"Expected <Dial>, got: {resp.text}"

        sip = dial.find("Sip")
        assert sip is not None, f"Expected <Sip> child of <Dial>, got: {resp.text}"
        assert sip.text == "sip:pstn-in-HAPPY@test-trunk.sip.livekit.cloud", (
            f"SIP URI should target the LiveKit room, got {sip.text!r}"
        )

        # Recording — same contract as /api/inbound-calls/connect
        assert dial.attrib.get("record") == "record-from-answer-dual", (
            f"record-from-answer-dual is the only acceptable value; got "
            f"{dial.attrib.get('record')!r}. Without it inbound calls have "
            f"NO audio captured."
        )
        cb = dial.attrib.get("recordingStatusCallback")
        assert cb and "/api/twilio-webhooks/recording" in cb, (
            f"recordingStatusCallback must point to recording webhook, got {cb!r}"
        )
        events = dial.attrib.get("recordingStatusCallbackEvent", "")
        assert "completed" in events and "failed" in events, (
            f"Must subscribe to completed+failed; got {events!r}"
        )

        # call_logs stamped — without user_id the row is invisible in the
        # owner's dashboard list (real bug pre-fix).
        log = patched_db["call_logs"].find_one({"call_sid": "CA_happy"})
        assert log is not None, "call_log row was not created"
        assert log["user_id"] == uid, (
            f"call_log.user_id must match the phone's owner ({uid}), got {log['user_id']}"
        )
        assert log["assistant_id"] == aid
        assert log["assistant_name"] == "Front Desk Bot"
        assert log["direction"] == "inbound"
        assert log["from_number"] == "+15551112222"
        assert log["to_number"] == "+15557776666"
        assert log["livekit_room"] == "pstn-in-HAPPY"

    def test_no_legacy_websocket_stream_emitted(
        self, client, patched_db, make_user, make_assistant, monkeypatch, webhook_env,
    ):
        """REGRESSION GUARD: pre-fix the /voice endpoint emitted
        `<Connect><Stream url='wss://.../api/inbound-calls/media-stream/{aid}'>`
        which pointed at a deleted route. Any future re-introduction of
        <Connect><Stream> here is a P0 production bug."""
        _stub_provision(monkeypatch, room_name="pstn-in-X")

        uid = make_user()
        aid = make_assistant(user_id=uid)
        patched_db["phone_numbers"].insert_one({
            "_id": ObjectId(),
            "user_id": uid,
            "phone_number": "+15554443333",
            "provider": "twilio",
            "provider_sid": "PNlegacy",
            "assigned_assistant_id": aid,
        })

        resp = client.post(
            "/api/twilio-webhooks/voice",
            data={"To": "+15554443333", "From": "+15551112222", "CallSid": "CA_legacy"},
        )

        text = resp.text
        assert "<Connect>" not in text, (
            f"<Connect> must not appear — legacy WebSocket pipeline. Got: {text}"
        )
        assert "media-stream" not in text, (
            f"'media-stream' substring must not appear — pointed at deleted "
            f"route in production. Got: {text}"
        )
        assert "wss://" not in text, (
            f"No WSS URI should appear — we use SIP for media now. Got: {text}"
        )

    def test_cross_tenant_to_does_not_leak_other_user(
        self, client, patched_db, make_user, make_assistant, monkeypatch, webhook_env,
    ):
        """Tenant A and tenant B both have numbers. A call to A's number
        MUST stamp the call_log with A's user_id, even if B's assistant
        somehow shares a sid (it shouldn't, but assert anyway).
        Defends against a class of cross-tenant leak bugs where the
        endpoint loaded the assistant from the wrong owner."""
        _stub_provision(monkeypatch, room_name="pstn-in-CT")

        a_uid = make_user(email="a@tenant.com")
        b_uid = make_user(email="b@tenant.com")
        a_aid = make_assistant(user_id=a_uid, name="Tenant A Bot")
        b_aid = make_assistant(user_id=b_uid, name="Tenant B Bot")

        patched_db["phone_numbers"].insert_many([
            {"_id": ObjectId(), "user_id": a_uid, "phone_number": "+15550000001",
             "provider": "twilio", "provider_sid": "PNa", "assigned_assistant_id": a_aid},
            {"_id": ObjectId(), "user_id": b_uid, "phone_number": "+15550000002",
             "provider": "twilio", "provider_sid": "PNb", "assigned_assistant_id": b_aid},
        ])

        resp = client.post(
            "/api/twilio-webhooks/voice",
            data={"To": "+15550000001", "From": "+15551112222", "CallSid": "CA_ct"},
        )
        assert resp.status_code == 200

        log = patched_db["call_logs"].find_one({"call_sid": "CA_ct"})
        assert log is not None
        assert log["user_id"] == a_uid, (
            f"call_log must belong to tenant A ({a_uid}), got {log['user_id']}"
        )
        assert log["assistant_id"] == a_aid
        assert log["assistant_name"] == "Tenant A Bot"
