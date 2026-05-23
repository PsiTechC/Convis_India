"""
Adversarial tests for the public demo-call surface
(`POST /api/public/demo-call/request-otp`, `POST /api/public/demo-call`).

These endpoints are unauthenticated and bill Twilio money on every call. The
mindset here is: an attacker on the open internet sees these endpoints — what
breaks?

Tests are organised by attack class:
  - Phone-number normalisation / injection
  - OTP-payload validation
  - X-Forwarded-For (rate-limit identity) trust
  - HTTP method / content-type / body-size confusion
  - Race conditions on Mongo-backed counters
  - Stale Twilio Verify Service SID handling
  - Premium-rate / sanctioned-prefix denylist completeness
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — mock the Twilio Verify layer so tests don't need real creds.
# ---------------------------------------------------------------------------

class _FakeTwilioVerifications:
    """Records send-OTP calls without actually sending anything."""
    last_sent: list = []
    raise_on_send: Exception | None = None

    def create(self, to=None, channel=None):
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.last_sent.append({"to": to, "channel": channel})
        m = MagicMock()
        m.status = "pending"
        return m


class _FakeTwilioVerificationChecks:
    last_checked: list = []
    next_status: str = "approved"
    raise_on_check: Exception | None = None

    def create(self, to=None, code=None):
        if self.raise_on_check is not None:
            raise self.raise_on_check
        self.last_checked.append({"to": to, "code": code})
        m = MagicMock()
        m.status = self.next_status
        return m


class _FakeService:
    def __init__(self):
        self._verifications = _FakeTwilioVerifications()
        self._checks = _FakeTwilioVerificationChecks()

    @property
    def verifications(self): return self._verifications

    @property
    def verification_checks(self): return self._checks

    def fetch(self):
        m = MagicMock()
        m.sid = "VAtest1234567890abcdef1234567890ab"
        return m


class _FakeServiceCollection:
    def __init__(self):
        self._service = _FakeService()
        self.created_count = 0

    def __call__(self, sid):
        return self._service

    def create(self, friendly_name=None, code_length=None):
        self.created_count += 1
        return MagicMock(sid="VAtest1234567890abcdef1234567890ab")


class _FakeTwilioClient:
    def __init__(self, *_a, **_k):
        self.verify = MagicMock()
        self.verify.v2 = MagicMock()
        self.verify.v2.services = _FakeServiceCollection()


@pytest.fixture
def patched_twilio(monkeypatch, patched_db):
    """Replace Twilio's REST client + creds resolver with deterministic fakes."""
    from app.routes.public import demo_call as dc

    # Bypass the encryption-layer creds resolver — tests don't have prod
    # ENCRYPTION_KEY anyway.
    monkeypatch.setattr(dc, "_get_demo_twilio_creds", lambda: ("ACfake", "fake_token"))
    # Replace the Client-class import inside the route module's locals.
    # Both `request_otp` and `demo_call` do `from twilio.rest import Client`
    # at function-scope, so we monkeypatch the import target.
    import twilio.rest as twilio_rest
    monkeypatch.setattr(twilio_rest, "Client", _FakeTwilioClient)
    # Pre-seed the cached Verify Service SID so nothing tries to hit Twilio
    # to bootstrap one.
    patched_db["settings"].insert_one({
        "_id": "demo_verify",
        "verify_service_sid": "VAtest1234567890abcdef1234567890ab",
        "created_at": datetime.now(timezone.utc),
    })
    # Seed the demo phone number record so the route can resolve it.
    from bson import ObjectId
    uid = ObjectId()
    aid = ObjectId()
    patched_db["users"].insert_one({"_id": uid, "email": "demo-owner@test.invalid"})
    patched_db["assistants"].insert_one({
        "_id": aid, "user_id": uid, "name": "Demo Assistant",
        "system_message": "You are the demo bot." * 200,  # > MIN
        "voice": "rachel", "temperature": 0.5, "llm_model": "gpt-4o-mini",
    })
    patched_db["phone_numbers"].insert_one({
        "_id": ObjectId(),
        "user_id": uid,
        "phone_number": "+16592655550",
        "provider": "twilio",
        "provider_sid": "PNtest",
        "assigned_assistant_id": aid,
        "assigned_assistant_name": "Demo Assistant",
        "status": "active",
    })
    return dc


# ---------------------------------------------------------------------------
# P0.3 — Phone-number normalisation / injection
# ---------------------------------------------------------------------------

class TestPhoneNumberHardening:
    """Probes `_normalize_phone` and the E164 regex for inputs that look
    benign but bypass intent (whitespace, control chars, unicode digits,
    way-too-long strings, premium prefixes the denylist missed)."""

    def test_phone_with_embedded_newline_is_rejected(self, client, patched_twilio):
        """An attacker injecting a newline could try to smuggle headers
        into the Twilio API call. Normaliser MUST strip control chars
        AND the resulting number must still pass E164 validation."""
        resp = client.post(
            "/api/public/demo-call/request-otp",
            json={"to_number": "+1415555\n0100"},
        )
        # Either the strip removes the newline cleanly, or the validator
        # rejects. NEVER 200 with an unsanitised newline making it to Twilio.
        assert resp.status_code in (200, 400), f"Got {resp.status_code}: {resp.text}"
        if resp.status_code == 200:
            # If accepted, the request-otp call into our fake must not
            # contain the newline character.
            sent = _FakeTwilioVerifications.last_sent
            assert sent, "Accepted request but didn't reach Twilio layer"
            assert "\n" not in (sent[-1]["to"] or ""), \
                f"Newline leaked into Twilio API call: {sent[-1]['to']!r}"

    def test_phone_with_null_byte_rejected(self, client, patched_twilio):
        resp = client.post(
            "/api/public/demo-call/request-otp",
            json={"to_number": "+14155551234\x00"},
        )
        assert resp.status_code != 200 or "\x00" not in (
            (_FakeTwilioVerifications.last_sent[-1]["to"] or "")
            if _FakeTwilioVerifications.last_sent else ""
        )

    def test_phone_with_unicode_arabic_indic_digits(self, client, patched_twilio):
        """Arabic-Indic digits ٠١٢٣٤٥٦٧٨٩ render visually similar to ASCII
        digits. If our normaliser strips +/digits using ASCII regex, these
        survive as garbage and reach Twilio."""
        resp = client.post(
            "/api/public/demo-call/request-otp",
            json={"to_number": "+١٤١٥٥٥٥١٢٣٤"},
        )
        # Should be REJECTED — E164 regex requires ASCII digits only.
        assert resp.status_code == 400, (
            f"Arabic-Indic digit number got {resp.status_code}; "
            f"E164 regex must enforce ASCII digits only. Body: {resp.text}"
        )

    def test_phone_full_width_digits_rejected(self, client, patched_twilio):
        """Full-width digits (U+FF10..U+FF19) — same threat class as Arabic-Indic."""
        resp = client.post(
            "/api/public/demo-call/request-otp",
            json={"to_number": "+14155551234"},  # ASCII baseline
        )
        # baseline must succeed
        assert resp.status_code in (200, 400, 503)
        # actual full-width
        resp2 = client.post(
            "/api/public/demo-call/request-otp",
            json={"to_number": "+１４１５５５５１２３４"},
        )
        assert resp2.status_code == 400, (
            f"Full-width digits got {resp2.status_code}; must reject."
        )

    def test_phone_over_15_digits_rejected(self, client, patched_twilio):
        """E.164 max is 15 digits. Longer = invalid + potential int overflow risk."""
        resp = client.post(
            "/api/public/demo-call/request-otp",
            json={"to_number": "+1234567890123456789"},  # 19 digits
        )
        assert resp.status_code == 400, f"Over-length number accepted: {resp.text}"

    def test_phone_just_plus_rejected(self, client, patched_twilio):
        for bad in ["+", "+0", "+1", "+ ", "  +  ", "++1234567890"]:
            resp = client.post(
                "/api/public/demo-call/request-otp",
                json={"to_number": bad},
            )
            assert resp.status_code == 400, (
                f"Pathological {bad!r} got {resp.status_code}: {resp.text}"
            )

    def test_phone_empty_string_rejected(self, client, patched_twilio):
        resp = client.post(
            "/api/public/demo-call/request-otp",
            json={"to_number": ""},
        )
        # Pydantic min_length isn't set so might 200/422 — must NOT be 200.
        assert resp.status_code != 200, f"Empty string accepted: {resp.text}"


# ---------------------------------------------------------------------------
# P0.2 — OTP-payload validation
# ---------------------------------------------------------------------------

class TestOtpValidation:
    """Adversarial inputs to /demo-call's `otp` field."""

    def test_otp_alpha_rejected(self, client, patched_twilio):
        resp = client.post(
            "/api/public/demo-call",
            json={"to_number": "+14155551234", "otp": "abcdef"},
        )
        assert resp.status_code == 400, (
            f"Alpha OTP accepted: {resp.status_code} {resp.text}"
        )

    def test_otp_unicode_digits_rejected(self, client, patched_twilio):
        # If the validator uses isdigit() (Python str method), unicode digits
        # like "१२३४५६" pass — but Twilio expects ASCII. Bug class: input that
        # validates locally but fails downstream OR mismatches the OTP that
        # was actually sent.
        resp = client.post(
            "/api/public/demo-call",
            json={"to_number": "+14155551234", "otp": "१२३४५६"},
        )
        # Either reject with 400, or accept and pass to Twilio cleanly. Must
        # not be a half-state where local validation passed but Twilio errors
        # surface as "code expired" (misleading).
        assert resp.status_code in (400, 401), (
            f"Unicode-digit OTP got {resp.status_code} — should be 400 strict-rejected."
        )

    def test_otp_over_max_length_rejected(self, client, patched_twilio):
        resp = client.post(
            "/api/public/demo-call",
            json={"to_number": "+14155551234", "otp": "1" * 50},
        )
        assert resp.status_code in (400, 422), (
            f"50-char OTP got {resp.status_code}; must be rejected by max_length."
        )

    def test_otp_with_whitespace_padding_handled(self, client, patched_twilio):
        """Users often paste an OTP with trailing whitespace from autofill.
        Our code does .strip() — verify that's actually invoked."""
        # We don't have a real OTP to verify; just check the strip happened
        # by inspecting the call to verification_checks (via fake).
        _FakeTwilioVerificationChecks.last_checked.clear()
        _FakeTwilioVerificationChecks.next_status = "approved"
        client.post(
            "/api/public/demo-call",
            json={"to_number": "+14155551234", "otp": "  123456  "},
        )
        if _FakeTwilioVerificationChecks.last_checked:
            checked = _FakeTwilioVerificationChecks.last_checked[-1]["code"]
            assert checked == "123456", (
                f"Whitespace not stripped before Twilio call: {checked!r}"
            )

    def test_otp_missing_field_pydantic_422(self, client, patched_twilio):
        """Field is required per Pydantic schema; missing → 422."""
        resp = client.post(
            "/api/public/demo-call",
            json={"to_number": "+14155551234"},
        )
        assert resp.status_code == 422, f"Missing OTP got {resp.status_code}"


# ---------------------------------------------------------------------------
# P0.1 — X-Forwarded-For spoofing (rate-limit-identity bypass)
# ---------------------------------------------------------------------------

class TestXForwardedForTrust:
    """The slowapi rate limiter and the per-IP Mongo counter both key on the
    REAL client IP, currently derived from `X-Forwarded-For` (first entry).

    The trade-off: this header is trivially spoofable by any HTTP client.
    BUT — at the App Runner edge, AWS rewrites/appends to X-F-F. Whether
    the FIRST entry is the original client OR an attacker-injected value
    depends on App Runner's behaviour. This test enumerates the assumption.
    """

    def test_xff_last_entry_is_treated_as_client(self, client, patched_twilio):
        """The LAST entry of X-Forwarded-For is the trusted-proxy-appended
        real TCP source. Earlier entries are attacker-controllable.

        Concrete: `X-F-F: 1.1.1.1, 2.2.2.2, 169.254.172.2` — the App Runner
        proxy appended `169.254.172.2`, so that's what we use as the rate-
        limit key. The `1.1.1.1` and `2.2.2.2` were set by client/upstream
        proxies and CANNOT be trusted on a public, cost-bearing endpoint."""
        from app.routes.public.demo_call import _client_ip
        from starlette.requests import Request as StarletteRequest

        # Multi-hop X-F-F (client → maybe-CF → App Runner → app)
        scope = {
            "type": "http",
            "method": "POST",
            "headers": [(b"x-forwarded-for", b"1.1.1.1, 2.2.2.2, 169.254.172.2")],
            "client": ("4.4.4.4", 12345),
        }
        req = StarletteRequest(scope)
        assert _client_ip(req) == "169.254.172.2", (
            f"_client_ip must return LAST X-F-F entry (App Runner appended), "
            f"got {_client_ip(req)!r}"
        )

    def test_xff_spoofing_does_NOT_multiply_quota(self, client, patched_twilio):
        """Attacker rotates X-F-F first-entry per request, hoping for fresh
        per-IP buckets. After the fix, all variants collapse to the same
        last-entry → same rate-limit key → the IP cap kicks in normally.

        With per-IP cap = 3/24h, 5 attempts from the same effective IP
        should produce 3 successes and 2 × 429."""
        successes = 0
        rate_limited = 0
        for i in range(5):
            resp = client.post(
                "/api/public/demo-call",
                json={"to_number": f"+14155550{i:03d}", "otp": "999999"},
                # Spoofed first entry rotates; real-trusted last entry stays.
                headers={"X-Forwarded-For": f"203.0.113.{i+1}, 169.254.172.5"},
            )
            if resp.status_code == 429:
                rate_limited += 1
            elif resp.status_code in (200, 401):
                # 401 = OTP rejected (expected — we send invalid otp), but it
                # still counted toward the per-IP attempt? Only successful
                # 200s count. If using Mongo cap directly, count attempts.
                successes += 1
        # The point: even if some variants succeed past the OTP layer (401)
        # vs hit the rate limit, the spoofed first-entry should NOT give
        # them 5 free buckets. Either rate_limited > 0, OR we need to test
        # a path that increments the Mongo counter.
        # Simpler proof: assert the IP-spoofed test no longer all-succeeds.
        # (Pre-fix: all 5 returned 200; post-fix: at most 3 do.)
        assert successes < 5, (
            f"X-F-F spoofing still multiplies quota — got {successes} "
            f"successes from 5 different first-entries. Last-entry should collapse."
        )


# ---------------------------------------------------------------------------
# P0.5 — Race condition on Mongo-backed rate-limit counters
# ---------------------------------------------------------------------------

class TestRateLimitRaceCondition:
    """The pattern `count_documents(...) >= LIMIT` then `insert_one(...)` is
    NOT atomic. Two simultaneous requests both see count=2 (under the 3-cap),
    both pass, both insert. End state: count=4, but limit was 3.

    Mongomock doesn't model race conditions. We simulate by pre-seeding the
    collection right at the boundary and verifying behaviour."""

    def test_count_then_insert_at_boundary(self, client, patched_twilio, patched_db):
        """At the boundary (already 3 calls today), the next call MUST 429."""
        from bson import ObjectId
        now = datetime.now(timezone.utc)
        for i in range(3):
            patched_db["demo_call_attempts"].insert_one({
                "_id": ObjectId(),
                "ip": "203.0.113.99",
                "to_number": "+14155551234",
                "created_at": now - timedelta(hours=1),
            })
        # 4th call from same number — should 429
        resp = client.post(
            "/api/public/demo-call",
            json={"to_number": "+14155551234", "otp": "123456"},
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
        assert resp.status_code == 429, (
            f"4th call/24h to same number should 429, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# P1.10 — HTTP method / content-type confusion
# ---------------------------------------------------------------------------

class TestHttpMethodContract:
    def test_get_on_post_endpoint(self, client, patched_twilio):
        resp = client.get("/api/public/demo-call")
        assert resp.status_code == 405, f"GET on POST-only got {resp.status_code}"

    def test_get_on_request_otp(self, client, patched_twilio):
        resp = client.get("/api/public/demo-call/request-otp")
        assert resp.status_code == 405

    def test_options_preflight_returns_cors(self, client, patched_twilio):
        """Browser preflight from convis.ai must return Access-Control-Allow-Origin."""
        resp = client.options(
            "/api/public/demo-call/request-otp",
            headers={
                "Origin": "https://www.convis.ai",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        assert resp.status_code in (200, 204), f"Preflight got {resp.status_code}"
        allow_origin = resp.headers.get("access-control-allow-origin")
        assert allow_origin in ("https://www.convis.ai", "*"), (
            f"CORS not configured for marketing site: {allow_origin!r}"
        )


# ---------------------------------------------------------------------------
# P2.13 — Premium-rate / sanctioned-prefix denylist completeness
# ---------------------------------------------------------------------------

class TestPremiumPrefixDenylist:
    """The current denylist covers a handful of US Caribbean and a few
    international premium ranges. Many high-cost / sanctioned prefixes
    (UK 09, Iran +98, North Korea +850, Cuba +53) are NOT blocked."""

    @pytest.mark.parametrize("number,country", [
        ("+850123456789", "North Korea"),         # Sanctioned
        ("+98123456789", "Iran"),                 # Sanctioned
        ("+5354012345", "Cuba"),                  # Sanctioned
        ("+44906012345", "UK premium-rate (09xx)"),  # ~£3.60/min
        ("+44904012345", "UK premium 0904"),
        ("+39199123456", "Italy premium 199"),    # ~€2/min
        ("+919876543210", "India regular — should be ALLOWED"),
    ])
    def test_high_cost_or_sanctioned_prefixes(self, client, patched_twilio, number, country):
        resp = client.post(
            "/api/public/demo-call/request-otp",
            json={"to_number": number},
        )
        is_india = number.startswith("+91")
        if is_india:
            assert resp.status_code in (200, 503), (
                f"{country} should be allowed, got {resp.status_code}"
            )
        else:
            assert resp.status_code == 400, (
                f"{country} ({number}) should be REJECTED to prevent toll fraud / "
                f"sanctions violation. Got {resp.status_code}: {resp.text}"
            )


# ---------------------------------------------------------------------------
# P2.14 — Mongo collection growth without TTL
# ---------------------------------------------------------------------------

class TestCollectionGrowthBounds:
    """`demo_otp_requests` has a 7-day TTL index (good). `demo_call_attempts`
    does NOT — it grows forever. With 100 demos/day cap, that's 36k rows/year.
    Negligible disk-wise, but slow `count_documents` queries over time."""

    def test_demo_call_attempts_should_have_ttl(self):
        """Declarative test: the index must be in INDEX_SPECS so every
        environment (prod + staging + test) gets it on startup."""
        from app.services.database_indexes import INDEX_SPECS
        ttl_specs = [
            spec for spec in INDEX_SPECS
            if spec[0] == "demo_call_attempts"
            and "expireAfterSeconds" in spec[3]
        ]
        assert ttl_specs, (
            "demo_call_attempts has NO TTL index in INDEX_SPECS — collection "
            "grows forever. Add to app/services/database_indexes.py: "
            "('demo_call_attempts', 'created_at', 'idx_demo_call_attempts_ttl', "
            "{'expireAfterSeconds': 30*24*3600})"
        )
        # Also verify a sane retention period (>=24h since limiter checks 24h,
        # but not absurdly long).
        ttl_seconds = ttl_specs[0][3]["expireAfterSeconds"]
        assert 24 * 3600 <= ttl_seconds <= 365 * 24 * 3600, (
            f"demo_call_attempts TTL of {ttl_seconds}s is outside [1d, 1y]"
        )


# ---------------------------------------------------------------------------
# Body-size / content-type confusion
# ---------------------------------------------------------------------------

class TestBodyParsing:
    def test_malformed_json_400(self, client, patched_twilio):
        resp = client.post(
            "/api/public/demo-call/request-otp",
            data="not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code in (400, 422), f"Got {resp.status_code}"

    def test_wrong_content_type_400(self, client, patched_twilio):
        """form-encoded body to a JSON endpoint."""
        resp = client.post(
            "/api/public/demo-call/request-otp",
            content=b"to_number=%2B14155551234",  # raw bytes (httpx-modern)
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # FastAPI/pydantic returns 422 because pydantic can't parse form as JSON
        assert resp.status_code in (400, 422), (
            f"Form-encoded body to JSON endpoint should 4xx, got {resp.status_code}"
        )

    def test_huge_body_handled(self, client, patched_twilio):
        """1 MB payload — body parser shouldn't crash."""
        garbage = "x" * (1024 * 1024)
        resp = client.post(
            "/api/public/demo-call/request-otp",
            json={"to_number": "+14155551234", "spam": garbage},
        )
        # Either rejected for unexpected fields OR succeeded ignoring extras.
        # Must NOT 500.
        assert resp.status_code < 500, f"Server crashed on large body: {resp.status_code}"
