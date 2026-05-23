"""Tests for the contact service.

Hits the survivors from QA review:
  • Phone normalization rejects malformed input (no leading +, garbage).
  • user_id coercion handles ObjectId / valid hex str / garbage.
  • set_do_not_remember cascade-deletes call_summaries on True transition.
  • set_do_not_remember on a different tenant returns "not found" (IDOR check).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from app.services.contact_service import (
    _coerce_user_id,
    _normalize_phone,
    set_do_not_remember,
)


# ── _normalize_phone: strict E.164 with leading + ────────────────────────────
class TestNormalizePhone:
    @pytest.mark.parametrize("inp,expected", [
        ("+14155550123", "+14155550123"),
        ("+91 88505-01889", "+918850501889"),
        ("+447911123456", "+447911123456"),
    ])
    def test_valid_e164_accepted(self, inp, expected):
        assert _normalize_phone(inp) == expected

    @pytest.mark.parametrize("inp", [
        "(415) 555-0123",            # no leading +
        "14155550123",                # no +, ambiguous
        "+1",                          # too short
        "+",                           # just plus
        "",                            # empty
        "   ",                         # whitespace
        None,                          # None
        42,                            # int
        "+0123456789",                 # leading 0 after + (invalid E.164)
        "+999999999999999999",         # too long
        "not a phone",                 # garbage
        '+14155550123"; DROP TABLE--', # injection
    ])
    def test_invalid_inputs_rejected(self, inp):
        assert _normalize_phone(inp) is None


# ── _coerce_user_id ──────────────────────────────────────────────────────────
class TestCoerceUserId:
    def test_objectid_passes_through(self):
        oid = ObjectId()
        assert _coerce_user_id(oid) == oid

    def test_valid_hex_string_converted(self):
        s = "64f342feb226fc2e0bd0f36d"
        out = _coerce_user_id(s)
        assert isinstance(out, ObjectId)
        assert str(out) == s

    @pytest.mark.parametrize("garbage", ["not-an-oid", "", None, 42, "abc"])
    def test_garbage_rejected(self, garbage):
        assert _coerce_user_id(garbage) is None


# ── set_do_not_remember: tenant-scoped cascade delete ────────────────────────
class TestSetDoNotRemember:
    @pytest.mark.asyncio
    async def test_invalid_user_id_short_circuits(self):
        out = await set_do_not_remember(
            contact_id=str(ObjectId()),
            user_id="not-an-objectid",
            do_not_remember=True,
        )
        assert out == {"ok": False, "deleted_summaries": 0, "error": "invalid_user_id"}

    @pytest.mark.asyncio
    async def test_invalid_contact_id_short_circuits(self):
        out = await set_do_not_remember(
            contact_id="abc",
            user_id=str(ObjectId()),
            do_not_remember=True,
        )
        assert out == {"ok": False, "deleted_summaries": 0, "error": "invalid_contact_id"}

    @pytest.mark.asyncio
    async def test_cascade_delete_fires_on_true(self):
        """The headline behaviour: do_not_remember=True must delete all
        existing summaries for the contact, in addition to flipping the flag."""
        uid = ObjectId()
        cid = ObjectId()

        # Mock Mongo: contact exists, 7 summaries exist.
        contacts_coll = MagicMock()
        contacts_coll.update_one.return_value = MagicMock(matched_count=1, modified_count=1)
        summaries_coll = MagicMock()
        summaries_coll.delete_many.return_value = MagicMock(deleted_count=7)
        db_mock = MagicMock()
        db_mock.__getitem__.side_effect = lambda k: contacts_coll if k == "contacts" else summaries_coll

        with patch("app.services.contact_service.Database.get_db", return_value=db_mock):
            out = await set_do_not_remember(
                contact_id=str(cid), user_id=str(uid), do_not_remember=True,
            )

        assert out == {"ok": True, "deleted_summaries": 7}
        # Confirm the delete_many was tenant-scoped (user_id in the filter).
        del_call = summaries_coll.delete_many.call_args
        assert del_call.args[0]["contact_id"] == cid
        assert del_call.args[0]["user_id"] == uid

    @pytest.mark.asyncio
    async def test_no_cascade_on_false(self):
        """Turning OFF do_not_remember must NOT delete anything — summaries
        from any prior history are already gone (from the prior ON-flip)."""
        uid = ObjectId()
        cid = ObjectId()

        contacts_coll = MagicMock()
        contacts_coll.update_one.return_value = MagicMock(matched_count=1, modified_count=1)
        summaries_coll = MagicMock()
        db_mock = MagicMock()
        db_mock.__getitem__.side_effect = lambda k: contacts_coll if k == "contacts" else summaries_coll

        with patch("app.services.contact_service.Database.get_db", return_value=db_mock):
            out = await set_do_not_remember(
                contact_id=str(cid), user_id=str(uid), do_not_remember=False,
            )

        assert out == {"ok": True, "deleted_summaries": 0}
        summaries_coll.delete_many.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_tenant_returns_not_found(self):
        """IDOR check: if (contact_id, user_id) don't match a row, the
        update_one matches 0 — we return an error rather than silently
        cascading-deleting someone else's data."""
        contacts_coll = MagicMock()
        contacts_coll.update_one.return_value = MagicMock(matched_count=0)
        summaries_coll = MagicMock()
        db_mock = MagicMock()
        db_mock.__getitem__.side_effect = lambda k: contacts_coll if k == "contacts" else summaries_coll

        with patch("app.services.contact_service.Database.get_db", return_value=db_mock):
            out = await set_do_not_remember(
                contact_id=str(ObjectId()), user_id=str(ObjectId()),
                do_not_remember=True,
            )

        assert out["ok"] is False
        assert out["error"] == "contact_not_found_or_wrong_tenant"
        # CRITICAL: no cross-tenant delete on a non-match.
        summaries_coll.delete_many.assert_not_called()
