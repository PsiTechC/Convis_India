"""
Adversarial security tests against routes in
`app/routes/inbound_calls/inbound_calls.py`.

Each test reproduces a candidate bug discovered during code review:
  - Two routes (recording-status, transcription-status) lack Twilio signature
    verification.
  - The /config/{assistant_id} route lacks any auth.
  - Recording webhook updates by call_sid alone with no direction filter.

Tests are written to FAIL on broken code (current state) and pass once the
issues are fixed. Each docstring contains the bug-report payload.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from bson import ObjectId


# ---------------------------------------------------------------------------
# P0/Critical: webhook routes accept unsigned requests
# ---------------------------------------------------------------------------

class TestRecordingStatusWebhookAuth:
    """
    BUG: /api/inbound-calls/recording-status accepts ANY POST without Twilio
    signature. An attacker can poison call_logs with arbitrary recording URLs
    and trigger AsyncInboundPostCallProcessor to fetch from attacker-controlled
    URLs.

    Severity: Critical
    Type: Security · SSRF · Data integrity
    """

    def test_recording_status_rejects_unsigned_request(
        self, client, patched_db, make_user, make_assistant, monkeypatch,
    ):
        # Re-enable signature verification (default-off in test env per conftest).
        monkeypatch.setenv("TWILIO_VERIFY_WEBHOOKS", "1")
        monkeypatch.setenv("ENVIRONMENT", "production")  # forces verify

        # Stage a victim call_log
        uid = make_user(email="victim@example.com")
        aid = make_assistant(user_id=uid)
        victim_sid = "CA_victim_abc"
        patched_db["call_logs"].insert_one({
            "call_sid": victim_sid,
            "user_id": uid,
            "assistant_id": aid,
            "direction": "inbound",
            "recording_url": None,
            "created_at": datetime.utcnow(),
        })

        # Attacker POSTs a recording-status with no signature
        resp = client.post(
            "/api/inbound-calls/recording-status",
            data={
                "CallSid": victim_sid,
                "RecordingStatus": "completed",
                "RecordingUrl": "https://attacker.evil/malicious.mp3",
                "RecordingSid": "RE_attacker",
                "RecordingDuration": "30",
            },
        )

        # EXPECTED: 403 Forbidden (missing X-Twilio-Signature header)
        assert resp.status_code == 403, (
            f"Expected 403 (signature required) but got {resp.status_code}. "
            f"Body: {resp.text}. This means the recording-status webhook is "
            f"unauthenticated and an attacker can poison call_logs with "
            f"arbitrary recording URLs."
        )

        # Verify the malicious URL was NOT stored
        log = patched_db["call_logs"].find_one({"call_sid": victim_sid})
        assert log["recording_url"] != "https://attacker.evil/malicious.mp3", (
            "Recording URL was overwritten by an unsigned request — "
            "call_log corrupted."
        )

    def test_transcription_status_rejects_unsigned_request(
        self, client, patched_db, make_user, make_assistant, monkeypatch,
    ):
        monkeypatch.setenv("TWILIO_VERIFY_WEBHOOKS", "1")
        monkeypatch.setenv("ENVIRONMENT", "production")

        uid = make_user(email="victim@example.com")
        aid = make_assistant(user_id=uid)
        victim_sid = "CA_victim_xyz"
        patched_db["call_logs"].insert_one({
            "call_sid": victim_sid,
            "user_id": uid,
            "assistant_id": aid,
            "transcription_text": None,
            "created_at": datetime.utcnow(),
        })

        resp = client.post(
            "/api/inbound-calls/transcription-status",
            data={
                "CallSid": victim_sid,
                "TranscriptionStatus": "completed",
                "TranscriptionText": "<script>alert(1)</script> attacker text",
                "TranscriptionSid": "TR_attacker",
            },
        )

        assert resp.status_code == 403, (
            f"Expected 403 (signature required) but got {resp.status_code}. "
            f"transcription-status accepts arbitrary text — XSS / fake "
            f"transcript injection."
        )

        log = patched_db["call_logs"].find_one({"call_sid": victim_sid})
        assert "<script>" not in (log.get("transcription_text") or ""), (
            "Attacker-controlled transcript was stored — XSS vector "
            "into the dashboard transcript view."
        )


# ---------------------------------------------------------------------------
# P0/Critical: /config/{assistant_id} leaks system_message without auth
# ---------------------------------------------------------------------------

class TestConfigEndpointAuth:
    """
    BUG: /api/inbound-calls/config/{assistant_id} returns the assistant's
    system_message, voice, and temperature for ANY caller — no auth, no
    ownership check. An attacker who learns/guesses an assistant_id can
    download the proprietary system prompt of any tenant.

    Severity: Major (IP leak); upgrade to Critical if prompts contain
    PII / business secrets / API keys (we have seen all three in prod
    system_messages).
    """

    def test_config_endpoint_requires_auth(
        self, client, patched_db, make_user, make_assistant,
    ):
        uid = make_user(email="victim@example.com")
        aid = make_assistant(
            user_id=uid,
            name="Confidential Bot",
            system_message=(
                "You are an internal HR assistant. Available salary bands: "
                "junior=$80k, senior=$220k. Reset code: SECRET123. "
                "Slack webhook: https://hooks.slack.com/services/T01/B02/abc"
            ),
        )

        # Anonymous probe — no token
        resp = client.get(f"/api/inbound-calls/config/{aid}")

        assert resp.status_code in (401, 403), (
            f"Expected 401/403 (auth required) but got {resp.status_code}. "
            f"Anonymous caller can download proprietary system_message."
        )

        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        leaked_message = body.get("config", {}).get("system_message", "")
        assert "SECRET123" not in leaked_message, (
            "Secrets from system_message leaked to anonymous caller."
        )

    def test_config_endpoint_requires_ownership(
        self, client, patched_db, make_user, make_assistant,
    ):
        # Two tenants
        owner = make_user(email="owner@example.com")
        attacker = make_user(email="attacker@example.com")
        aid = make_assistant(user_id=owner, system_message="OWNER-ONLY-DATA")

        from tests.qa_audit.conftest import auth_headers
        resp = client.get(
            f"/api/inbound-calls/config/{aid}",
            headers=auth_headers(str(attacker)),
        )
        assert resp.status_code in (403, 404), (
            f"Expected 403/404 — attacker authenticated but should not see "
            f"another tenant's assistant. Got {resp.status_code}: {resp.text}"
        )
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        leaked = body.get("config", {}).get("system_message", "") if isinstance(body, dict) else ""
        assert "OWNER-ONLY-DATA" not in leaked, (
            "Cross-tenant assistant prompt leak via /config/{assistant_id}."
        )


# ---------------------------------------------------------------------------
# P1/Major: recording-status updates by call_sid only — no direction filter
# ---------------------------------------------------------------------------

class TestRecordingStatusDirectionFilter:
    """
    BUG: /api/inbound-calls/recording-status updates call_logs by call_sid
    alone. Combined with the missing-signature bug above, an attacker who
    learns an OUTBOUND call_sid (same Twilio account) can hijack the
    outbound recording too.

    Severity: Major (escalates the unsigned-webhook bug to outbound calls).
    """

    def test_recording_webhook_only_updates_inbound_logs(
        self, client, patched_db, make_user, make_assistant, monkeypatch,
    ):
        # Test in dev mode (signature off) to isolate this distinct bug.
        monkeypatch.setenv("TWILIO_VERIFY_WEBHOOKS", "0")

        uid = make_user(email="u@test.invalid")
        aid = make_assistant(user_id=uid)
        outbound_sid = "CA_outbound_001"
        patched_db["call_logs"].insert_one({
            "call_sid": outbound_sid,
            "user_id": uid,
            "assistant_id": aid,
            "direction": "outbound",
            "recording_url": "https://twilio.com/legit-outbound.mp3",
        })

        # Attacker fires the inbound recording webhook with the outbound SID.
        resp = client.post(
            "/api/inbound-calls/recording-status",
            data={
                "CallSid": outbound_sid,
                "RecordingStatus": "completed",
                "RecordingUrl": "https://attacker.evil/outbound-hijack.mp3",
                "RecordingSid": "RE_attack",
                "RecordingDuration": "10",
            },
        )
        # Either reject by status, or refuse to update (we accept either as a fix).
        log = patched_db["call_logs"].find_one({"call_sid": outbound_sid})
        assert log["recording_url"] != "https://attacker.evil/outbound-hijack.mp3", (
            "Inbound recording webhook hijacked an outbound call_log "
            "(no direction filter on the update)."
        )
