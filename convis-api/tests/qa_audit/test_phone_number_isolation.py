"""
Tests around the recently-shipped phone-number tenant isolation fixes:
  - /connect/preview returns availability flags
  - /assign-assistant rejects cross-tenant attempts (IDOR)
  - /call-logs/user/{user_id} enforces ownership
  - Twilio voice-status `upsert=True` creates orphan rows (KNOWN BUG, still present)
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId

from tests.qa_audit.conftest import auth_headers, make_jwt


# ---------------------------------------------------------------------------
# /connect/preview availability filter (positive test for recent fix)
# ---------------------------------------------------------------------------

class TestConnectPreviewAvailability:
    """Verify the preview annotates each Twilio number with its availability."""

    def test_preview_marks_available_owned_self_owned_other(
        self, client, patched_db, make_user, monkeypatch,
    ):
        # Two users in the system
        me = make_user(email="me@test.invalid")
        other = make_user(email="other@test.invalid")

        # Three Twilio SIDs:
        #   sid_AVAIL   — not in DB
        #   sid_SELF    — in DB owned by me
        #   sid_OTHER   — in DB owned by other
        patched_db["phone_numbers"].insert_one({
            "_id": ObjectId(),
            "user_id": me,
            "phone_number": "+15551110000",
            "provider_sid": "PN_self_xxx",
        })
        patched_db["phone_numbers"].insert_one({
            "_id": ObjectId(),
            "user_id": other,
            "phone_number": "+15552220000",
            "provider_sid": "PN_other_yyy",
        })

        # Mock Twilio's incoming_phone_numbers.list()
        from app.routes.phone_numbers import phone_numbers as pn_module

        twilio_records = []
        for sid, phone in [
            ("PN_avail_zzz", "+15553330000"),
            ("PN_self_xxx", "+15551110000"),
            ("PN_other_yyy", "+15552220000"),
        ]:
            r = MagicMock()
            r.sid = sid
            r.phone_number = phone
            r.friendly_name = phone
            r.capabilities = {"voice": True, "sms": False, "mms": False}
            twilio_records.append(r)

        class FakeTwilioClient:
            def __init__(self, *a, **k): pass
            @property
            def incoming_phone_numbers(self):
                m = MagicMock()
                m.list = lambda **kw: twilio_records
                return m

        monkeypatch.setattr(pn_module, "Client", FakeTwilioClient)

        resp = client.post(
            "/api/phone-numbers/connect/preview",
            headers=auth_headers(str(me)),
            json={
                "provider": "twilio",
                "account_sid": "AC" + "x" * 32,
                "auth_token": "x" * 30,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Availability must be reported for each number
        by_sid = {n["sid"]: n for n in body["numbers"]}
        assert by_sid["PN_avail_zzz"]["availability"] == "available"
        assert by_sid["PN_self_xxx"]["availability"] == "owned_by_self"
        assert by_sid["PN_other_yyy"]["availability"] == "owned_by_other"
        assert by_sid["PN_other_yyy"]["owner_email"] == "other@test.invalid", (
            "Owner email must be reported for cross-tenant numbers so the user "
            "knows who to contact for transfer. Got "
            f"{by_sid['PN_other_yyy'].get('owner_email')!r}."
        )

        # Counts
        assert body["available_count"] == 1
        assert body["owned_by_self_count"] == 1
        assert body["owned_by_other_count"] == 1


# ---------------------------------------------------------------------------
# /assign-assistant cross-tenant IDOR
# ---------------------------------------------------------------------------

class TestAssignAssistantIDOR:
    """Caller must own BOTH the phone number and the assistant. Otherwise:
      - someone else's number → 404 (no probe leak)
      - someone else's assistant → 404 (no probe leak)
    """

    def test_attacker_cannot_assign_to_victims_phone(
        self, client, patched_db, make_user, make_assistant,
    ):
        victim = make_user(email="victim@test.invalid")
        attacker = make_user(email="attacker@test.invalid")

        victim_phone = ObjectId()
        patched_db["phone_numbers"].insert_one({
            "_id": victim_phone,
            "user_id": victim,
            "phone_number": "+15551112222",
            "provider": "twilio",
            "provider_sid": "PN_victim",
            "capabilities": {"voice": True, "sms": False, "mms": False},
        })
        attacker_assistant = make_assistant(user_id=attacker, name="Attacker Bot")

        resp = client.post(
            "/api/phone-numbers/assign-assistant",
            headers=auth_headers(str(attacker)),
            json={
                "phone_number_id": str(victim_phone),
                "assistant_id": str(attacker_assistant),
            },
        )
        # 404 (not 403) prevents probe-based existence detection of victim's number.
        assert resp.status_code == 404, (
            f"Attacker assigning to victim's phone must 404, got "
            f"{resp.status_code}: {resp.text}"
        )

        # Verify: victim's phone is unchanged
        doc = patched_db["phone_numbers"].find_one({"_id": victim_phone})
        assert doc.get("assigned_assistant_id") is None, (
            "Victim's phone number was assigned despite IDOR check — "
            f"now points to {doc.get('assigned_assistant_id')}"
        )

    def test_attacker_cannot_assign_victims_assistant(
        self, client, patched_db, make_user, make_assistant,
    ):
        victim = make_user(email="victim@test.invalid")
        attacker = make_user(email="attacker@test.invalid")

        attacker_phone = ObjectId()
        patched_db["phone_numbers"].insert_one({
            "_id": attacker_phone,
            "user_id": attacker,
            "phone_number": "+15553334444",
            "provider": "twilio",
            "provider_sid": "PN_attacker",
            "capabilities": {"voice": True, "sms": False, "mms": False},
        })
        victim_assistant = make_assistant(user_id=victim, name="Victim Bot")

        resp = client.post(
            "/api/phone-numbers/assign-assistant",
            headers=auth_headers(str(attacker)),
            json={
                "phone_number_id": str(attacker_phone),
                "assistant_id": str(victim_assistant),
            },
        )
        assert resp.status_code == 404, (
            f"Attacker using victim's assistant must 404, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Twilio voice-status webhook creates orphan rows (KNOWN, NOT YET FIXED)
# ---------------------------------------------------------------------------

class TestVoiceStatusOrphans:
    """
    Regression guard for the orphan-row bug:
    /api/twilio-webhooks/voice-status used to upsert=True with no
    direction/user_id filter, accumulating orphan call_log rows missing the
    fields the dashboard filters on. Production previously had 1,619 such
    orphans. The fix dropped upsert=True so unknown CallSids are ignored.
    """

    def test_voice_status_does_not_create_orphan(
        self, client, patched_db, monkeypatch,
    ):
        monkeypatch.setenv("TWILIO_VERIFY_WEBHOOKS", "0")

        unknown_call_sid = "CA_brand_new_abc123"
        assert patched_db["call_logs"].find_one({"call_sid": unknown_call_sid}) is None

        resp = client.post(
            "/api/twilio-webhooks/voice-status",
            data={
                "CallSid": unknown_call_sid,
                "CallStatus": "completed",
                "CallDuration": "30",
                "From": "+15551110000",
                "To": "+15552220000",
            },
        )
        assert resp.status_code == 200

        log = patched_db["call_logs"].find_one({"call_sid": unknown_call_sid})
        assert log is None, (
            f"REGRESSION: voice-status webhook created an orphan call_log for "
            f"an unknown CallSid. Doc: {log}. The fix drops upsert=True so "
            f"only pre-existing rows get updated; unknown CallSids must be "
            f"ignored (and logged as a warning), not turned into rows missing "
            f"user_id/assistant_id/direction."
        )

    def test_voice_status_updates_existing_log(
        self, client, patched_db, make_user, make_assistant, monkeypatch,
    ):
        """Positive case — the webhook STILL updates a pre-existing call_log
        for a known CallSid. We didn't break the legitimate happy path."""
        monkeypatch.setenv("TWILIO_VERIFY_WEBHOOKS", "0")

        uid = make_user()
        aid = make_assistant(user_id=uid)
        known_sid = "CA_known_xyz"
        patched_db["call_logs"].insert_one({
            "call_sid": known_sid,
            "user_id": uid,
            "assistant_id": aid,
            "direction": "inbound",
            "status": "ringing",
            "duration": None,
            "created_at": datetime.utcnow(),
        })

        resp = client.post(
            "/api/twilio-webhooks/voice-status",
            data={
                "CallSid": known_sid,
                "CallStatus": "completed",
                "CallDuration": "42",
            },
        )
        assert resp.status_code == 200
        log = patched_db["call_logs"].find_one({"call_sid": known_sid})
        assert log["status"] == "completed"
        assert log["duration"] == 42
