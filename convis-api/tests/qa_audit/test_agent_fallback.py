"""
Verify the defensive change to `_lookup_assistant_for_number` in
agent_worker.py — when multiple tenants have docs for the same phone number
with conflicting assignments, the function MUST refuse to route rather than
guess and risk leaking a caller into the wrong tenant's bot.
"""
from __future__ import annotations

from unittest.mock import patch
from bson import ObjectId
import pytest


@pytest.fixture
def stub_database(monkeypatch):
    """The agent_worker module pulls Database lazily; we patch the lookup."""
    from app.config import database as db_module
    storage = []

    class FakeColl:
        def find(self, query=None):
            q = query or {}
            results = []
            for d in storage:
                ok = True
                for k, v in q.items():
                    if d.get(k) != v:
                        ok = False; break
                if ok: results.append(d)
            return results

    class FakeDB(dict):
        def __getitem__(self, k):
            return FakeColl()

    monkeypatch.setattr(db_module.Database, "get_db", classmethod(lambda cls: FakeDB()))
    return storage


def test_returns_none_when_zero_docs(stub_database):
    from app.services.livekit.agent_worker import _lookup_assistant_for_number
    assert _lookup_assistant_for_number("+15555550000") is None


def test_returns_assistant_when_unambiguous(stub_database, monkeypatch):
    from app.services.livekit import agent_worker
    expected = ObjectId()
    docs = [{"phone_number": "+15551111111", "assigned_assistant_id": expected}]
    # Patch the .find() call directly via module-level swap
    class _F:
        def find(self, q=None):
            return [d for d in docs if d.get("phone_number") == q.get("phone_number")]
    class _DB(dict):
        def __getitem__(self, k): return _F()
    monkeypatch.setattr(agent_worker, "logger", agent_worker.logger)  # no-op
    from app.config import database as db_module
    monkeypatch.setattr(db_module.Database, "get_db", classmethod(lambda cls: _DB()))

    assert agent_worker._lookup_assistant_for_number("+15551111111") == str(expected)


def test_refuses_ambiguous_assignments(monkeypatch):
    """Two docs same phone_number, DIFFERENT assistants → must return None."""
    from app.services.livekit import agent_worker
    docs = [
        {"phone_number": "+15552222222", "assigned_assistant_id": ObjectId()},
        {"phone_number": "+15552222222", "assigned_assistant_id": ObjectId()},
    ]
    class _F:
        def find(self, q=None):
            return [d for d in docs if d.get("phone_number") == q.get("phone_number")]
    class _DB(dict):
        def __getitem__(self, k): return _F()
    from app.config import database as db_module
    monkeypatch.setattr(db_module.Database, "get_db", classmethod(lambda cls: _DB()))

    result = agent_worker._lookup_assistant_for_number("+15552222222")
    assert result is None, (
        f"Ambiguous lookup MUST return None to avoid wrong-tenant routing, "
        f"got {result!r}. This was the post-dedupe defensive fix — regression "
        f"means cross-tenant call-routing is back."
    )


def test_returns_unique_when_multiple_docs_same_assistant(monkeypatch):
    """Multiple docs all pointing to the SAME assistant → safe to return."""
    from app.services.livekit import agent_worker
    expected = ObjectId()
    docs = [
        {"phone_number": "+15553333333", "assigned_assistant_id": expected},
        {"phone_number": "+15553333333", "assigned_assistant_id": expected},
    ]
    class _F:
        def find(self, q=None):
            return [d for d in docs if d.get("phone_number") == q.get("phone_number")]
    class _DB(dict):
        def __getitem__(self, k): return _F()
    from app.config import database as db_module
    monkeypatch.setattr(db_module.Database, "get_db", classmethod(lambda cls: _DB()))

    assert agent_worker._lookup_assistant_for_number("+15553333333") == str(expected)


def test_returns_none_when_doc_exists_but_no_assignment(monkeypatch):
    from app.services.livekit import agent_worker
    docs = [{"phone_number": "+15554444444", "assigned_assistant_id": None}]
    class _F:
        def find(self, q=None):
            return [d for d in docs if d.get("phone_number") == q.get("phone_number")]
    class _DB(dict):
        def __getitem__(self, k): return _F()
    from app.config import database as db_module
    monkeypatch.setattr(db_module.Database, "get_db", classmethod(lambda cls: _DB()))

    assert agent_worker._lookup_assistant_for_number("+15554444444") is None
