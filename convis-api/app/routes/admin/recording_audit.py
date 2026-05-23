"""Admin diagnostic: report recording coverage across recent call_logs.

Answers the question "are all my Twilio calls actually getting recorded?"
without needing direct Mongo access. Returns per-call status:
  - has_recording_url: True/False
  - recording_status: completed | failed | (null if no callback fired)
  - direction, provider, call_sid

Plus aggregate counts so you can spot trends — e.g. if recording success
rate dips below ~95% it's worth investigating Twilio-side.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from app.config.database import Database
from app.utils.auth import get_current_user, require_admin

logger = logging.getLogger(__name__)

router = APIRouter()


class CallRecordingStatus(BaseModel):
    call_sid: Optional[str]
    direction: Optional[str]
    provider: Optional[str]
    from_number: Optional[str]
    to_number: Optional[str]
    duration: Optional[int]
    has_recording_url: bool
    recording_status: Optional[str]
    created_at: Optional[str]


class RecordingAuditResponse(BaseModel):
    window_hours: int
    total_calls: int
    completed_calls: int
    with_recording_url: int
    without_recording_url: int
    coverage_pct: float
    by_direction: dict
    last_calls: List[CallRecordingStatus]


@router.get(
    "/recording-audit",
    response_model=RecordingAuditResponse,
    status_code=status.HTTP_200_OK,
)
async def recording_audit(
    hours: int = Query(24, ge=1, le=168, description="Look-back window in hours"),
    limit: int = Query(20, ge=1, le=200, description="How many recent calls to detail"),
    current_user: dict = Depends(get_current_user),
):
    """Per-call recording status + aggregate coverage stats.

    Coverage is "of the calls that COMPLETED, what fraction have a
    recording_url stamped on the call_logs row." Calls that never reached
    completed status (still ringing / failed-before-answer) are excluded
    from the percentage so we're not punished for unanswered campaign dials.
    """
    require_admin(current_user)

    db = Database.get_db()
    window_start = datetime.now(timezone.utc) - timedelta(hours=hours)

    cursor = db["call_logs"].find(
        {"created_at": {"$gte": window_start}},
        {
            "call_sid": 1, "direction": 1, "from_number": 1, "to_number": 1,
            "duration": 1, "status": 1, "provider": 1,
            "recording_url": 1, "recording_status": 1,
            "created_at": 1,
        },
    ).sort("created_at", -1)

    rows = list(cursor)
    total = len(rows)

    completed_rows = [r for r in rows if (r.get("status") or "").lower() in ("completed", "answered")]
    with_url = sum(1 for r in completed_rows if r.get("recording_url"))
    without_url = len(completed_rows) - with_url
    coverage_pct = round(100.0 * with_url / len(completed_rows), 2) if completed_rows else 0.0

    by_direction: dict = {}
    for r in completed_rows:
        d = (r.get("direction") or "unknown").lower()
        slot = by_direction.setdefault(d, {"completed": 0, "with_recording": 0})
        slot["completed"] += 1
        if r.get("recording_url"):
            slot["with_recording"] += 1

    last = [
        CallRecordingStatus(
            call_sid=r.get("call_sid"),
            direction=r.get("direction"),
            provider=r.get("provider"),
            from_number=r.get("from_number"),
            to_number=r.get("to_number"),
            duration=r.get("duration"),
            has_recording_url=bool(r.get("recording_url")),
            recording_status=r.get("recording_status"),
            created_at=r["created_at"].isoformat() if r.get("created_at") else None,
        )
        for r in rows[:limit]
    ]

    return RecordingAuditResponse(
        window_hours=hours,
        total_calls=total,
        completed_calls=len(completed_rows),
        with_recording_url=with_url,
        without_recording_url=without_url,
        coverage_pct=coverage_pct,
        by_direction=by_direction,
        last_calls=last,
    )
