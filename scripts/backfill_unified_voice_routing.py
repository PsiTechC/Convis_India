#!/usr/bin/env python3
"""One-shot backfill: align every Twilio number under Convis management
to the unified voice-routing webhook (`/api/twilio-webhooks/voice`).

What this fixes
---------------
Numbers purchased / imported before the unified-routing refactor may have
their Twilio `voice_url` or `voice_application_sid` set to:
  - the legacy `/api/inbound-calls/incoming-call/{assistant_id}` per-assistant URL
  - a TwiML Application SID pointing at a deprecated WebSocket pipeline
  - nothing at all (silent ringing → hangup)

After this script:
  - voice_url   = `https://api.convis.ai/api/twilio-webhooks/voice`
  - voice_application_sid = `''` (cleared)
  - status_callback = `https://api.convis.ai/api/twilio-webhooks/voice-status`

Inbound call routing is then driven entirely by the dynamic webhook, which
looks up the assigned assistant per-call from Mongo's `assigned_assistant_id`
on the `phone_numbers` doc.

Usage
-----
This script must be run on a host that has:
  - access to Mongo (production URI in convis-api/.env)
  - the production `ENCRYPTION_KEY` (to decrypt stored Twilio creds)

Recommended: run it inside the running App Runner container via SSM, OR via
a one-off ECS task on the same VPC. Local runs from a dev box typically
fail decryption.

    cd convis-api && python ../scripts/backfill_unified_voice_routing.py

Add `--dry-run` to print what WOULD change without touching Twilio.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make the convis-api app importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "convis-api"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / "convis-api" / ".env")

from pymongo import MongoClient  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    ap.add_argument("--limit", type=int, default=0, help="Cap the number of records processed (0=all)")
    args = ap.parse_args()

    # Late imports — these need env vars + sys.path
    from app.utils.twilio_voice_routing import ensure_unified_voice_routing, unified_voice_url
    from app.utils.twilio_helpers import decrypt_twilio_credentials, CredentialDecryptionError
    from twilio.rest import Client

    voice_url = unified_voice_url()
    if not voice_url:
        print("FATAL: API_BASE_URL or BASE_URL must be set in env so we can compute the webhook URL.")
        sys.exit(1)
    print(f"Target voice_url: {voice_url}\n")

    db = MongoClient(os.environ["MONGODB_URI"])[os.environ["DATABASE_NAME"]]
    twilio_phones = list(
        db["phone_numbers"].find(
            {"provider": "twilio", "provider_sid": {"$exists": True, "$ne": None}},
            {"_id": 1, "user_id": 1, "phone_number": 1, "provider_sid": 1, "assigned_assistant_id": 1},
        )
    )
    print(f"Found {len(twilio_phones)} Twilio number(s) under management.\n")
    if args.limit and len(twilio_phones) > args.limit:
        twilio_phones = twilio_phones[: args.limit]
        print(f"  (capped to first {args.limit} by --limit)\n")

    # Group by owning user_id so we use one Twilio Client per user.
    by_user: dict[str, list[dict]] = {}
    for p in twilio_phones:
        by_user.setdefault(str(p["user_id"]), []).append(p)

    summary = {"updated": 0, "failed": 0, "skipped": 0}
    for uid, phones in by_user.items():
        conn = db["provider_connections"].find_one({"user_id": phones[0]["user_id"], "provider": "twilio"})
        if not conn:
            print(f"⚠ user {uid}: no Twilio provider_connection — skipping {len(phones)} numbers")
            summary["skipped"] += len(phones)
            continue
        try:
            sid, token = decrypt_twilio_credentials(conn)
        except CredentialDecryptionError as exc:
            print(f"⚠ user {uid}: cannot decrypt creds ({exc}) — skipping {len(phones)} numbers")
            summary["skipped"] += len(phones)
            continue
        if not (sid and token):
            print(f"⚠ user {uid}: blank creds — skipping {len(phones)} numbers")
            summary["skipped"] += len(phones)
            continue

        client = Client(sid, token)
        u_email = (db["users"].find_one({"_id": phones[0]["user_id"]}, {"email": 1}) or {}).get("email", "?")
        print(f"User {u_email} ({uid}): {len(phones)} number(s)")

        for p in phones:
            label = p.get("phone_number", p["provider_sid"])
            if args.dry_run:
                # Just fetch current state to see what's there
                try:
                    current = client.incoming_phone_numbers(p["provider_sid"]).fetch()
                    needs_update = (
                        current.voice_url != voice_url
                        or (current.voice_application_sid or "") != ""
                    )
                    state = "WOULD UPDATE" if needs_update else "already aligned"
                    print(f"  {label}: {state}  (cur voice_url={current.voice_url!r}, app_sid={current.voice_application_sid!r})")
                    if needs_update:
                        summary["updated"] += 1
                    else:
                        summary["skipped"] += 1
                except Exception as exc:
                    print(f"  {label}: ERROR fetching — {exc}")
                    summary["failed"] += 1
            else:
                ok, msg = ensure_unified_voice_routing(client, p["provider_sid"], label=label)
                if ok:
                    print(f"  {label}: ✓ {msg}")
                    summary["updated"] += 1
                else:
                    print(f"  {label}: ✗ {msg}")
                    summary["failed"] += 1

    print()
    print("=" * 60)
    print(f"Summary: updated={summary['updated']}  failed={summary['failed']}  skipped={summary['skipped']}")
    if args.dry_run:
        print("(--dry-run: no changes were written to Twilio)")
    print("=" * 60)


if __name__ == "__main__":
    main()
