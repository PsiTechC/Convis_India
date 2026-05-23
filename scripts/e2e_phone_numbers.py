#!/usr/bin/env python3
"""End-to-end phone-number test runner.

For every number in the admin user's account:
  - INBOUND: verify Twilio's voice_url is wired (via the dashboard API's
    webhook_url field — that's exactly what the assignment route writes
    to Twilio), then prompt the user to place a manual call so we can
    confirm the agent actually answers.
  - OUTBOUND: place a real outbound call via /api/outbound-calls/dial
    to the user's test phone, poll call_logs, report status.

Usage:
    cd /Users/psitech/Desktop/Psitech/Convis-main
    python3 scripts/e2e_phone_numbers.py

You'll be prompted for:
  - API base URL (default: api.convis.ai or App Runner direct)
  - Admin JWT (from browser DevTools → Application → Cookies → 'token',
    OR localStorage 'token' on webapp.convis.ai)
  - Your test phone number in E.164 (e.g. +15551234567)

Twilio billing: ~$0.01-0.05 per outbound call attempt. With 3 numbers
that's ~$0.15 worst case for a full run.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


# ── ANSI helpers ───────────────────────────────────────────────────────────
def c(code, s): return f"\033[{code}m{s}\033[0m"
ok = lambda s: c(32, s); bad = lambda s: c(31, s); warn = lambda s: c(33, s)
hi = lambda s: c(36, s); dim = lambda s: c(90, s)


def http(method: str, url: str, token: str | None = None, body: dict | None = None) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"raw": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def main():
    print(hi("\n=== Convis Phone-Number E2E Test ==="))

    api_base = prompt("API base URL", "https://api.convis.ai").rstrip("/")
    token = prompt("Admin JWT (paste from browser)").strip()
    if not token:
        print(bad("\nNo JWT provided. Get one from:"))
        print("  1. Open https://webapp.convis.ai in your browser")
        print("  2. Log in as admin")
        print("  3. DevTools → Application → Local Storage → 'token' (or Cookies → 'token')")
        sys.exit(1)
    test_phone = prompt("Your test phone number (E.164)", "+15551234567").strip()
    if not re.fullmatch(r"\+[1-9]\d{6,14}", test_phone):
        print(bad(f"Invalid E.164 phone: {test_phone}")); sys.exit(1)

    # ── 0. Resolve admin user_id from JWT (decode without verify — we only
    #     need the clientId field; the API verifies it for real on every call) ──
    try:
        import base64
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        user_id = payload.get("clientId")
        email = payload.get("email", "?")
        role = payload.get("role", "?")
    except Exception as e:
        print(bad(f"Couldn't parse JWT: {e}")); sys.exit(1)
    if not user_id:
        print(bad("JWT has no clientId. Wrong token?")); sys.exit(1)
    print(f"\nAuthenticated as: {hi(email)} (role={role}, user_id={user_id})")

    # ── 1. Fetch phone-numbers list ──
    print(dim("\n[1/3] Fetching your phone numbers..."))
    status, body = http("GET", f"{api_base}/api/phone-numbers/user/{user_id}", token=token)
    if status != 200:
        print(bad(f"  Failed ({status}): {body}")); sys.exit(1)
    numbers = body.get("phone_numbers") or body.get("numbers") or []
    if not numbers:
        print(warn("  No phone numbers in your account.")); sys.exit(0)
    print(ok(f"  Found {len(numbers)} number(s)\n"))

    expected_inbound_path = "/api/inbound-calls/connect/"
    results = []  # per-number outcome

    for i, n in enumerate(numbers, 1):
        phone = n.get("phone_number") or n.get("phoneNumber")
        phone_id = n.get("id") or n.get("_id")
        assistant_id = n.get("assigned_assistant_id") or n.get("assignedAssistantId")
        assistant_name = n.get("assigned_assistant_name") or n.get("assignedAssistantName") or "—"
        webhook = n.get("webhook_url") or n.get("webhookUrl")
        status_active = (n.get("status") or "active").lower() == "active"

        print(hi(f"━━━ #{i}: {phone}  ({assistant_name}) ━━━"))

        # ── INBOUND wiring check ──
        inbound_ok = (
            status_active
            and assistant_id
            and webhook
            and expected_inbound_path in webhook
            and assistant_id in webhook
        )
        if inbound_ok:
            print(ok(f"  INBOUND  ✓ webhook → {webhook}"))
        else:
            issues = []
            if not status_active: issues.append("number not active")
            if not assistant_id: issues.append("no assistant assigned")
            if not webhook: issues.append("webhook_url missing")
            elif expected_inbound_path not in webhook: issues.append(f"wrong path: {webhook}")
            elif assistant_id and assistant_id not in webhook: issues.append("webhook points to different assistant")
            print(bad(f"  INBOUND  ✗ " + " · ".join(issues)))

        # ── OUTBOUND test: place a real call ──
        if not assistant_id:
            print(bad("  OUTBOUND ✗ skipped — no assistant assigned"))
            results.append((phone, inbound_ok, False, "no assistant"))
            print()
            continue

        print(dim(f"  OUTBOUND … placing call to {test_phone}"))
        dial_body = {
            "assistant_id": assistant_id,
            "phone_number": test_phone,
            "from_phone_number_id": phone_id,
        }
        s, b = http("POST", f"{api_base}/api/outbound-calls/dial", token=token, body=dial_body)
        if s != 200:
            print(bad(f"  OUTBOUND ✗ dial API returned {s}: {b}"))
            results.append((phone, inbound_ok, False, f"dial {s}"))
            print()
            continue

        call_sid = b.get("call_sid") or b.get("callSid")
        print(ok(f"  OUTBOUND … dial accepted (call_sid={call_sid})"))

        # Poll call_logs for ~30s to see the call status update.
        final_status = None
        for poll in range(15):
            time.sleep(2)
            sl, lb = http("GET", f"{api_base}/api/phone-numbers/call-logs/user/{user_id}?limit=5", token=token)
            if sl != 200:
                continue
            logs = lb.get("call_logs") or lb.get("calls") or []
            for log in logs:
                if log.get("call_sid") == call_sid or log.get("twilio_call_sid") == call_sid:
                    final_status = log.get("status") or log.get("call_status")
                    break
            if final_status in ("completed", "answered", "in-progress", "ringing"):
                break

        if final_status in ("completed", "answered", "in-progress", "ringing"):
            print(ok(f"  OUTBOUND ✓ status={final_status}"))
            results.append((phone, inbound_ok, True, final_status))
        elif final_status:
            print(warn(f"  OUTBOUND ⚠ status={final_status} (didn't reach 'answered'/'completed' in 30s)"))
            results.append((phone, inbound_ok, False, final_status))
        else:
            print(warn("  OUTBOUND ⚠ no status update visible in 30s — call may still be in progress"))
            results.append((phone, inbound_ok, False, "timeout"))
        print()

    # ── Manual inbound prompt ──
    print(hi("━━━ INBOUND manual verification ━━━"))
    print(f"Now please call EACH of these numbers from your phone ({test_phone}):")
    for n in numbers:
        print(f"  • {n.get('phone_number')}  →  expected agent: {n.get('assigned_assistant_name', '—')}")
    input("\nAfter you've called each one, press Enter to verify…")

    print(dim("\n[3/3] Checking call_logs for inbound calls from your number…"))
    sl, lb = http("GET", f"{api_base}/api/phone-numbers/call-logs/user/{user_id}?limit=50", token=token)
    inbound_seen = {}
    if sl == 200:
        for log in (lb.get("call_logs") or []):
            if log.get("direction") == "inbound" and (log.get("from_number") == test_phone):
                inbound_seen[log.get("to_number")] = log.get("status") or log.get("call_status")

    # ── Report ──
    print(hi("\n━━━━━━━━━━━━━━━ FINAL REPORT ━━━━━━━━━━━━━━━"))
    print(f"{'Number':<18} {'Inbound (config)':<20} {'Inbound (actual)':<22} {'Outbound':<14}")
    for phone, in_ok, out_ok, out_detail in results:
        in_actual = inbound_seen.get(phone)
        in_cfg_str = ok("✓ wired") if in_ok else bad("✗ broken")
        in_act_str = ok(f"✓ {in_actual}") if in_actual else warn("— no call rcvd")
        out_str = ok(f"✓ {out_detail}") if out_ok else bad(f"✗ {out_detail}")
        print(f"{phone:<18} {in_cfg_str:<30} {in_act_str:<32} {out_str}")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
