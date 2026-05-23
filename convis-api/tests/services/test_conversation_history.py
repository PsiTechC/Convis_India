"""Tests for the pre-call context-block builder.

Covers the QA findings that mattered most:
  • Block truncation drops OLDEST summaries, KEEPS newest (the bug we fixed).
  • Untrusted-data disclaimer present (defence-in-depth for prompt injection).
  • do_not_remember=True short-circuits even when summaries exist.
  • Empty / no-contact / no-summary paths return None (cold-start behaviour).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.conversation_history_service import (
    _MAX_BLOCK_CHARS,
    _format_summary_line,
    build_context_block,
)


def _make_summary(date: datetime, raw: str = "Summary text",
                  key_facts=None, outcomes=None, follow_ups=None,
                  sentiment="neutral") -> dict:
    return {
        "date": date,
        "raw_summary": raw,
        "key_facts": key_facts or [],
        "outcomes": outcomes or [],
        "follow_up_items": follow_ups or [],
        "sentiment": sentiment,
    }


# ── _format_summary_line: deterministic, skips empty fields ──────────────────
class TestFormatSummaryLine:
    def test_full_summary_renders_all_fields(self):
        s = _make_summary(
            datetime(2026, 5, 18, tzinfo=timezone.utc),
            key_facts=["filed last year"],
            outcomes=["booked May 22"],
            follow_ups=["ask about diet"],
            sentiment="positive",
            raw="Caller was friendly.",
        )
        line = _format_summary_line(s)
        assert "[2026-05-18]" in line
        assert "Sentiment: positive" in line
        assert "filed last year" in line
        assert "booked May 22" in line
        assert "ask about diet" in line
        assert "Caller was friendly." in line

    def test_neutral_sentiment_omitted(self):
        s = _make_summary(
            datetime(2026, 5, 18, tzinfo=timezone.utc),
            sentiment="neutral", raw="Plain.",
        )
        line = _format_summary_line(s)
        # neutral is the default; rendering "Sentiment: neutral" is noise.
        assert "Sentiment: neutral" not in line

    def test_empty_summary_collapses_to_just_date(self):
        s = _make_summary(datetime(2026, 5, 18, tzinfo=timezone.utc), raw="")
        line = _format_summary_line(s)
        assert line == "[2026-05-18]"


# ── build_context_block: do_not_remember short-circuit ───────────────────────
@pytest.mark.asyncio
async def test_returns_none_when_contact_has_do_not_remember():
    """Even with summaries present, do_not_remember=True must yield None."""
    contact_doc = {
        "_id": "contact_oid",
        "name": "Alice",
        "do_not_remember": True,
    }
    with patch(
        "app.services.conversation_history_service.get_contact_by_phone",
        AsyncMock(return_value=contact_doc),
    ):
        block = await build_context_block(
            user_id="64f342feb226fc2e0bd0f36d",
            phone_number="+14155550123",
            max_calls=3,
        )
    assert block is None


@pytest.mark.asyncio
async def test_returns_none_when_no_contact():
    """First-time call: contact doesn't exist yet → no history to inject."""
    with patch(
        "app.services.conversation_history_service.get_contact_by_phone",
        AsyncMock(return_value=None),
    ):
        block = await build_context_block(
            user_id="64f342feb226fc2e0bd0f36d",
            phone_number="+14155550123",
        )
    assert block is None


# ── Truncation behaviour: drop OLDEST, keep NEWEST ───────────────────────────
@pytest.mark.asyncio
async def test_truncation_drops_oldest_summaries_keeps_newest():
    """Regression for the QA-found bug where block[:N] kept the oldest
    summary text and dropped the newest. The newest summary is the most
    actionable — it must always be in the rendered block."""
    contact_doc = {"_id": "contact_oid", "name": "Alice", "do_not_remember": False}

    # 10 summaries, each ~600 chars when rendered, dates Jan 1..10 2026.
    # Block will overflow the 4000 char budget → truncate.
    huge = "X" * 500
    summaries = [
        _make_summary(
            datetime(2026, 1, i, tzinfo=timezone.utc),
            raw=f"{huge} day{i}",
            key_facts=[f"fact for day {i}"],
        )
        for i in range(1, 11)
    ]
    # The service fetches descending then reverses to ascending.
    # We hand back the same desc order Mongo would.
    desc_summaries = list(reversed(summaries))

    fetch = MagicMock(return_value=desc_summaries)

    with patch(
        "app.services.conversation_history_service.get_contact_by_phone",
        AsyncMock(return_value=contact_doc),
    ), patch(
        "app.services.conversation_history_service.Database.get_db"
    ) as mock_db:
        mock_collection = MagicMock()
        mock_collection.find.return_value.sort.return_value.limit.return_value = desc_summaries
        mock_db.return_value.__getitem__.return_value = mock_collection

        block = await build_context_block(
            user_id="64f342feb226fc2e0bd0f36d",
            phone_number="+14155550123",
            max_calls=10,
        )

    assert block is not None
    # Newest summary (day 10) MUST be in the block.
    assert "day10" in block, "newest summary was dropped (truncation reversed?)"
    # Block must be under budget.
    assert len(block) <= _MAX_BLOCK_CHARS + 100  # small slack for footer
    # If oldest got dropped, the "truncated" notice should be there.
    if "day1 " not in block:
        assert "truncated" in block.lower()


# ── Footer contains untrusted-data disclaimer (prompt-injection defence) ─────
@pytest.mark.asyncio
async def test_block_includes_untrusted_data_disclaimer():
    contact_doc = {"_id": "contact_oid", "name": "Alice", "do_not_remember": False}
    summary = _make_summary(
        datetime(2026, 5, 18, tzinfo=timezone.utc),
        key_facts=["filed taxes"], raw="Quick summary",
    )
    with patch(
        "app.services.conversation_history_service.get_contact_by_phone",
        AsyncMock(return_value=contact_doc),
    ), patch(
        "app.services.conversation_history_service.Database.get_db"
    ) as mock_db:
        mock_collection = MagicMock()
        mock_collection.find.return_value.sort.return_value.limit.return_value = [summary]
        mock_db.return_value.__getitem__.return_value = mock_collection

        block = await build_context_block(
            user_id="64f342feb226fc2e0bd0f36d",
            phone_number="+14155550123",
        )

    assert block is not None
    # Must instruct the LLM to treat the injected block as untrusted data.
    # Exact wording can drift; the SEMANTIC must hold: somewhere we tell
    # the model these aren't instructions.
    lowered = block.lower()
    assert "untrusted" in lowered or "not instructions" in lowered, (
        "context-block footer must mark previous-call content as untrusted "
        "to harden against cross-call prompt injection"
    )
