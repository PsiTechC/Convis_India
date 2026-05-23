"""One-shot admin endpoint: apply per-assistant turn-detection profiles.

Two named profiles:
  - "snappy"  — for B2B sales, support, receptionist, demo. Faster turns,
                occasional cut-off on long mid-sentence pauses.
  - "patient" — for elderly care, healthcare intake, slow speakers. Never
                interrupts, ~700ms slower replies.

Hardcoded routing rule: assistants whose name contains "care", "elderly",
"senior", "patient" (case-insensitive) get the "patient" profile; everyone
else gets "snappy". The rule deliberately catches Care Companion (the
assistant we identified as too aggressive in QA).

Override per-assistant by passing `assistant_overrides` in the request
body — { "<assistant_id>": "snappy" | "patient" }.

Idempotent. Safe to re-run. Compares before/after and only writes when the
target value differs (avoids needlessly bumping `updated_at`).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.config.database import Database
from app.utils.auth import get_current_user, require_admin

logger = logging.getLogger(__name__)

router = APIRouter()


# Profile values are aligned with CLAUDE.md's documented "reliable" + "patient"
# presets. Snappy is faster than the current code defaults but more accurate
# (asr_endpointing_ms=200 instead of 130, which the docs warn is "riskier").
PROFILES: Dict[str, Dict[str, float]] = {
    "snappy": {
        "asr_endpointing_ms": 200,
        "min_endpointing_delay": 0.15,
        "min_interruption_duration": 0.25,
    },
    "patient": {
        "asr_endpointing_ms": 300,
        "min_endpointing_delay": 0.20,
        "min_interruption_duration": 0.60,
    },
}

PATIENT_KEYWORDS = ("care", "elderly", "senior", "patient", "geriatric")


def _pick_profile(name: Optional[str]) -> str:
    n = (name or "").lower()
    return "patient" if any(k in n for k in PATIENT_KEYWORDS) else "snappy"


class ProfileRequest(BaseModel):
    dry_run: bool = False
    assistant_overrides: Dict[str, str] = Field(default_factory=dict)


class PerAssistantReport(BaseModel):
    assistant_id: str
    name: Optional[str]
    profile: str
    before: Dict[str, Optional[float]]
    after: Dict[str, float]
    changed: bool


class ProfileResponse(BaseModel):
    total: int
    changed: int
    unchanged: int
    by_profile: Dict[str, int]
    profiles: Dict[str, Dict[str, float]]
    results: List[PerAssistantReport]


@router.post(
    "/apply-endpointing-profiles",
    response_model=ProfileResponse,
    status_code=status.HTTP_200_OK,
)
async def apply_endpointing_profiles(
    body: ProfileRequest,
    current_user: dict = Depends(get_current_user),
):
    require_admin(current_user)

    db = Database.get_db()
    assistants = list(db["assistants"].find(
        {},
        {"_id": 1, "name": 1, "asr_endpointing_ms": 1,
         "min_endpointing_delay": 1, "min_interruption_duration": 1},
    ))

    overrides = {k.lower(): v.lower() for k, v in body.assistant_overrides.items()}
    by_profile = {"snappy": 0, "patient": 0}
    results: List[PerAssistantReport] = []
    changed = 0

    for a in assistants:
        aid = str(a["_id"])
        name = a.get("name")
        profile_name = overrides.get(aid.lower()) or _pick_profile(name)
        if profile_name not in PROFILES:
            profile_name = "snappy"
        target = PROFILES[profile_name]

        before = {
            "asr_endpointing_ms": a.get("asr_endpointing_ms"),
            "min_endpointing_delay": a.get("min_endpointing_delay"),
            "min_interruption_duration": a.get("min_interruption_duration"),
        }
        is_changed = any(before.get(k) != target[k] for k in target)
        by_profile[profile_name] += 1

        if is_changed and not body.dry_run:
            db["assistants"].update_one(
                {"_id": a["_id"]},
                {"$set": target},
            )
            changed += 1
        elif is_changed:
            changed += 1  # would-change in dry-run

        results.append(PerAssistantReport(
            assistant_id=aid, name=name, profile=profile_name,
            before=before, after=target, changed=is_changed,
        ))

    logger.info(
        "[ADMIN_PROFILES] %s applied. total=%d changed=%d snappy=%d patient=%d "
        "triggered_by=%s",
        "dry-run" if body.dry_run else "live",
        len(assistants), changed, by_profile["snappy"], by_profile["patient"],
        current_user.get("user_id"),
    )

    return ProfileResponse(
        total=len(assistants),
        changed=changed,
        unchanged=len(assistants) - changed,
        by_profile=by_profile,
        profiles=PROFILES,
        results=results,
    )
