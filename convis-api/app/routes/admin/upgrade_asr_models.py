"""One-shot admin endpoint: upgrade outdated Deepgram ASR model selections
on existing assistants.

Why this exists
---------------
Several assistants in production are configured with vanilla `nova-2` instead
of `nova-2-phonecall`. The vanilla model is tuned for studio audio and is
markedly slower / less reliable on 8kHz PSTN audio (e.g. one Care Companion
call this afternoon clocked 3.66s of transcription_delay on a 24-character
utterance — pure Deepgram processing time).

Vanilla `nova-2` and `nova-2-phonecall` cost the same on Deepgram's pricing,
so this is a free reliability+latency upgrade with no downside. Older
`enhanced` and `base` model selections are also superseded by nova-2/3.

Idempotent. Per-assistant report shows old + new for audit.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.config.database import Database
from app.utils.auth import get_current_user, require_admin

logger = logging.getLogger(__name__)

router = APIRouter()


# Map of "outdated model name" → "preferred replacement". Only models in this
# map get rewritten — anything not listed (e.g. nova-3, custom Deepgram models,
# or a model the operator deliberately set) is left alone.
DEFAULT_UPGRADES: Dict[str, str] = {
    "nova-2": "nova-2-phonecall",  # tuned for 8kHz PSTN, same price
    "nova-2-general": "nova-2-phonecall",
    "enhanced": "nova-2-phonecall",  # legacy model — deprecated by Deepgram
    "base": "nova-2-phonecall",      # legacy model — deprecated by Deepgram
}


class UpgradeRequest(BaseModel):
    dry_run: bool = False
    upgrades: Optional[Dict[str, str]] = Field(
        default=None,
        description=(
            "Optional override map: {old_model: new_model}. If omitted, the "
            "default upgrade map is used (nova-2 -> nova-2-phonecall, etc.)."
        ),
    )
    assistant_overrides: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional per-assistant overrides: {assistant_id: target_model}. "
            "Forces a specific model regardless of current value."
        ),
    )


class PerAssistantUpgrade(BaseModel):
    assistant_id: str
    name: Optional[str]
    before: Optional[str]
    after: Optional[str]
    changed: bool
    reason: str


class UpgradeResponse(BaseModel):
    total: int
    changed: int
    unchanged: int
    by_target_model: Dict[str, int]
    upgrade_map_used: Dict[str, str]
    results: List[PerAssistantUpgrade]


@router.post(
    "/upgrade-asr-models",
    response_model=UpgradeResponse,
    status_code=status.HTTP_200_OK,
)
async def upgrade_asr_models(
    body: UpgradeRequest,
    current_user: dict = Depends(get_current_user),
):
    """Walk every assistant and rewrite outdated `asr_model` fields.

    Default behaviour (no body): nova-2 / nova-2-general / enhanced / base
    all get rewritten to `nova-2-phonecall`. Anything else is left alone.

    Pass `assistant_overrides` to force a specific model on specific
    assistants (e.g. force Care Companion to nova-3 if phonecall variant
    still flakes).
    """
    require_admin(current_user)

    upgrade_map = {**DEFAULT_UPGRADES, **(body.upgrades or {})}
    overrides = {k.lower(): v for k, v in body.assistant_overrides.items()}

    db = Database.get_db()
    assistants = list(db["assistants"].find(
        {},
        {"_id": 1, "name": 1, "asr_model": 1,
         "asr_language": 1, "multilingual": 1},
    ))

    by_target: Dict[str, int] = {}
    results: List[PerAssistantUpgrade] = []
    changed = 0

    # Languages safely served by `nova-2-phonecall` (English-only model).
    # Anything else MUST stay on a multi-lang capable model (nova-2 or nova-3)
    # otherwise Deepgram silently transcribes garbage or rejects the session.
    PHONECALL_SAFE_LANGS = {"en", "en-us", "en-gb", "en-au", "en-in", "en-nz"}

    for a in assistants:
        aid = str(a["_id"])
        name = a.get("name")
        current_model = a.get("asr_model")
        is_multilingual = bool(a.get("multilingual"))
        lang = (a.get("asr_language") or "en").lower()

        # Override ALWAYS wins — operator explicitly chose this. Skip the
        # safety checks because the operator presumably knows what they're
        # doing for this specific assistant.
        forced = overrides.get(aid.lower())
        if forced:
            target_model = forced
            reason = "explicit override"
        elif is_multilingual:
            # Runtime forces nova-3 for multilingual — Mongo asr_model is
            # ignored. Skip the rewrite to avoid noisy Mongo writes.
            target_model = current_model
            reason = "multilingual=True (runtime forces nova-3, skipped)"
        elif current_model in upgrade_map:
            target_value = upgrade_map[current_model]
            # Block phonecall on non-English assistants — phonecall is en-only.
            if "phonecall" in target_value and lang not in PHONECALL_SAFE_LANGS:
                target_model = current_model
                reason = (
                    f"target {target_value!r} is English-only but assistant "
                    f"asr_language={lang!r} — skipped to preserve transcription"
                )
            else:
                target_model = target_value
                reason = f"auto-upgrade {current_model} → {target_model}"
        elif current_model is None:
            target_model = None
            reason = "uses code default (nova-2-phonecall) — no change needed"
        else:
            target_model = current_model
            reason = f"current value {current_model!r} not in upgrade map"

        is_changed = target_model is not None and target_model != current_model
        if target_model is not None:
            by_target[target_model] = by_target.get(target_model, 0) + 1

        if is_changed and not body.dry_run:
            db["assistants"].update_one(
                {"_id": a["_id"]},
                {"$set": {"asr_model": target_model}},
            )
            changed += 1
        elif is_changed:
            changed += 1  # would-change in dry-run

        results.append(PerAssistantUpgrade(
            assistant_id=aid, name=name,
            before=current_model, after=target_model,
            changed=is_changed, reason=reason,
        ))

    logger.info(
        "[ADMIN_ASR_UPGRADE] %s applied. total=%d changed=%d "
        "by_target=%s triggered_by=%s",
        "dry-run" if body.dry_run else "live",
        len(assistants), changed, by_target, current_user.get("user_id"),
    )

    return UpgradeResponse(
        total=len(assistants),
        changed=changed,
        unchanged=len(assistants) - changed,
        by_target_model=by_target,
        upgrade_map_used=upgrade_map,
        results=results,
    )
