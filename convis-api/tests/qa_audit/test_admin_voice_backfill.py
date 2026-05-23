"""
Tests for the admin-only POST /api/admin/backfill-twilio-voice-routing
endpoint. Adversarial focus:

- Auth: a non-admin JWT MUST get 403, not the backfill report.
- No api_base_url: 500 with a clear message — refusing to write junk URLs
  is safer than running and corrupting voice_url across the fleet.
- Per-tenant isolation: a Twilio Client is opened with EACH tenant's
  decrypted creds; one tenant's outage does not abort the whole run.
- dry_run: never invokes ensure_unified_voice_routing on Twilio.
- Idempotency: the helper is the single mutation point and is mocked here;
  re-running the route is a no-op insofar as it issues the same updates.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from bson import ObjectId

from tests.qa_audit.conftest import auth_headers, make_jwt


def _seed_two_tenants(patched_db):
    a_uid = ObjectId()
    b_uid = ObjectId()
    patched_db["users"].insert_many([
        {"_id": a_uid, "email": "a@x.com", "role": "client", "verified": True},
        {"_id": b_uid, "email": "b@x.com", "role": "client", "verified": True},
    ])
    # Two phones per tenant
    patched_db["phone_numbers"].insert_many([
        {"_id": ObjectId(), "user_id": a_uid, "phone_number": "+15550000001",
         "provider": "twilio", "provider_sid": "PNa1"},
        {"_id": ObjectId(), "user_id": a_uid, "phone_number": "+15550000002",
         "provider": "twilio", "provider_sid": "PNa2"},
        {"_id": ObjectId(), "user_id": b_uid, "phone_number": "+15550000003",
         "provider": "twilio", "provider_sid": "PNb1"},
        {"_id": ObjectId(), "user_id": b_uid, "phone_number": "+15550000004",
         "provider": "twilio", "provider_sid": "PNb2"},
    ])
    patched_db["provider_connections"].insert_many([
        {"user_id": a_uid, "provider": "twilio",
         "account_sid": "enc:ACa", "auth_token": "enc:tokenA"},
        {"user_id": b_uid, "provider": "twilio",
         "account_sid": "enc:ACb", "auth_token": "enc:tokenB"},
    ])
    return a_uid, b_uid


@pytest.fixture
def admin_env(monkeypatch):
    """Make sure unified_voice_url() returns a real URL during the test —
    otherwise the route returns 500 by design and the success-path
    assertions don't apply."""
    from app.config import settings as settings_mod
    monkeypatch.setattr(settings_mod.settings, "api_base_url", "https://api.convis.test")
    monkeypatch.setattr(settings_mod.settings, "base_url", None)


class TestAdminAuth:
    def test_non_admin_jwt_gets_403(self, client, patched_db, make_user, admin_env):
        """REGRESSION GUARD: this endpoint touches Twilio config across every
        tenant. If a normal-user JWT could call it, ANY logged-in user could
        rewrite voice_url on every customer's numbers. MUST be 403."""
        uid = make_user(email="non-admin@test.invalid")
        # default make_user is role=client → JWT will carry token_role="user"
        # (user role mapping happens in get_current_user).
        resp = client.post(
            "/api/admin/backfill-twilio-voice-routing",
            headers=auth_headers(str(uid), role="user"),
        )
        assert resp.status_code == 403, (
            f"Non-admin must get 403, got {resp.status_code}: {resp.text}"
        )

    def test_no_jwt_gets_401(self, client, patched_db, admin_env):
        resp = client.post("/api/admin/backfill-twilio-voice-routing")
        assert resp.status_code in (401, 403), (
            f"Unauthenticated must be rejected, got {resp.status_code}"
        )


class TestApiBaseUrlGate:
    def test_500_when_api_base_url_unset(self, client, patched_db, make_user, monkeypatch):
        """If api_base_url is unset we'd compute target=None and write
        garbage. Endpoint MUST refuse with 500 + a clear message."""
        from app.config import settings as settings_mod
        monkeypatch.setattr(settings_mod.settings, "api_base_url", None)
        monkeypatch.setattr(settings_mod.settings, "base_url", None)

        uid = make_user()
        resp = client.post(
            "/api/admin/backfill-twilio-voice-routing",
            headers=auth_headers(str(uid), role="admin"),
        )
        assert resp.status_code == 500
        assert "API_BASE_URL" in resp.text
        # No phone records were touched — verified indirectly: the route
        # short-circuits before opening any Twilio client.


class TestDryRun:
    def test_dry_run_does_not_call_helper(
        self, client, patched_db, make_user, admin_env,
    ):
        """dry_run=true must never reach ensure_unified_voice_routing.
        We patch the helper to assert it's NOT called."""
        admin_uid = make_user(email="ops@convis.ai")
        _seed_two_tenants(patched_db)

        with patch(
            "app.routes.admin.voice_routing_backfill.ensure_unified_voice_routing"
        ) as mock_helper, patch(
            "app.routes.admin.voice_routing_backfill.decrypt_twilio_credentials",
            return_value=("ACtest", "token"),
        ):
            resp = client.post(
                "/api/admin/backfill-twilio-voice-routing?dry_run=true",
                headers=auth_headers(str(admin_uid), role="admin"),
            )

        assert resp.status_code == 200, resp.text
        assert mock_helper.call_count == 0, (
            f"dry_run must NOT invoke the Twilio update helper. "
            f"Was called {mock_helper.call_count} times."
        )

        body = resp.json()
        assert body["total"] == 4
        assert body["updated"] == 4  # in dry-run, "updated" tallies the would-changes
        assert body["target_voice_url"] == "https://api.convis.test/api/twilio-webhooks/voice"
        # Every record carries the WOULD-update message
        assert all("WOULD update" in r["message"] for r in body["results"])


class TestLiveRun:
    def test_live_run_calls_helper_per_number(
        self, client, patched_db, make_user, admin_env,
    ):
        admin_uid = make_user(email="ops@convis.ai")
        _seed_two_tenants(patched_db)

        with patch(
            "app.routes.admin.voice_routing_backfill.ensure_unified_voice_routing",
            return_value=(True, "ok"),
        ) as mock_helper, patch(
            "app.routes.admin.voice_routing_backfill.decrypt_twilio_credentials",
            return_value=("ACtest", "token"),
        ):
            resp = client.post(
                "/api/admin/backfill-twilio-voice-routing",
                headers=auth_headers(str(admin_uid), role="admin"),
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 4
        assert body["updated"] == 4
        assert body["failed"] == 0
        assert mock_helper.call_count == 4

        called_sids = {c.args[1] for c in mock_helper.call_args_list}
        assert called_sids == {"PNa1", "PNa2", "PNb1", "PNb2"}

    def test_one_tenant_decryption_failure_does_not_abort_others(
        self, client, patched_db, make_user, admin_env,
    ):
        """REGRESSION GUARD: if tenant A's credentials can't decrypt, we
        skip A's numbers but STILL process tenant B. One tenant's broken
        provider_connection must not orphan the whole fleet."""
        from app.utils.twilio_helpers import CredentialDecryptionError

        admin_uid = make_user(email="ops@convis.ai")
        a_uid, b_uid = _seed_two_tenants(patched_db)

        def _decrypt_selective(conn):
            if conn["user_id"] == a_uid:
                raise CredentialDecryptionError("key mismatch for tenant A")
            return ("ACb", "tokenB")

        with patch(
            "app.routes.admin.voice_routing_backfill.ensure_unified_voice_routing",
            return_value=(True, "ok"),
        ) as mock_helper, patch(
            "app.routes.admin.voice_routing_backfill.decrypt_twilio_credentials",
            side_effect=_decrypt_selective,
        ):
            resp = client.post(
                "/api/admin/backfill-twilio-voice-routing",
                headers=auth_headers(str(admin_uid), role="admin"),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 4
        assert body["skipped"] == 2  # tenant A's two numbers
        assert body["updated"] == 2  # tenant B's two numbers got fixed
        # Helper invoked for B's two SIDs only
        called_sids = {c.args[1] for c in mock_helper.call_args_list}
        assert called_sids == {"PNb1", "PNb2"}, (
            f"Tenant A's failure must not affect tenant B. Called: {called_sids}"
        )

    def test_one_number_twilio_failure_does_not_abort_loop(
        self, client, patched_db, make_user, admin_env,
    ):
        """If the helper returns (False, ...) on number 2 of 4, numbers 1, 3,
        and 4 must still be processed — and number 2 reported as failed."""
        admin_uid = make_user(email="ops@convis.ai")
        _seed_two_tenants(patched_db)

        def _helper(client_obj, sid, *, label=""):
            if sid == "PNa2":
                return (False, "HTTP 429 Too Many Requests")
            return (True, "ok")

        with patch(
            "app.routes.admin.voice_routing_backfill.ensure_unified_voice_routing",
            side_effect=_helper,
        ) as mock_helper, patch(
            "app.routes.admin.voice_routing_backfill.decrypt_twilio_credentials",
            return_value=("ACtest", "token"),
        ):
            resp = client.post(
                "/api/admin/backfill-twilio-voice-routing",
                headers=auth_headers(str(admin_uid), role="admin"),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 4
        assert body["updated"] == 3
        assert body["failed"] == 1
        # All four were attempted
        called_sids = {c.args[1] for c in mock_helper.call_args_list}
        assert called_sids == {"PNa1", "PNa2", "PNb1", "PNb2"}
        # PNa2 row reports the Twilio error verbatim
        pna2 = next(r for r in body["results"] if r["provider_sid"] == "PNa2")
        assert pna2["ok"] is False
        assert "429" in pna2["message"]


class TestNoTwilioNumbers:
    def test_empty_fleet_returns_zeros(
        self, client, patched_db, make_user, admin_env,
    ):
        admin_uid = make_user(email="ops@convis.ai")
        # No phone_numbers seeded
        resp = client.post(
            "/api/admin/backfill-twilio-voice-routing",
            headers=auth_headers(str(admin_uid), role="admin"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["updated"] == 0
        assert body["failed"] == 0
        assert body["skipped"] == 0
        assert body["results"] == []
