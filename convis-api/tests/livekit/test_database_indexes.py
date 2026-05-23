"""
Bug guarded: previous implementation called create_index per spec inside one
big try/except. A single conflict on call_logs.call_sid would log an error,
fall through, and main.py would still print '✅ created/verified'. Future
indexes silently weren't created.

Now: each spec is isolated; failures aggregate; main.py logs a distinct ❌.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pymongo.errors import OperationFailure


def _make_db_with_failures(*fail_index_names: str):
    """Return a fake db whose .create_index raises OperationFailure for the
    given index names and succeeds for everything else."""
    fail_set = set(fail_index_names)

    collections: dict[str, MagicMock] = {}

    def _make_coll(name: str) -> MagicMock:
        coll = MagicMock(name=f"Collection({name!r})")

        def _create_index(_keys, name=None, **_kwargs):
            if name in fail_set:
                raise OperationFailure(
                    "An existing index has the same name as the requested index",
                    code=86,
                    details={"errmsg": "IndexKeySpecsConflict"},
                )
            return name

        coll.create_index.side_effect = _create_index
        return coll

    db = MagicMock()
    db.__getitem__.side_effect = lambda n: collections.setdefault(n, _make_coll(n))
    return db


def test_create_all_indexes_returns_true_when_clean():
    from app.services import database_indexes as di

    db = _make_db_with_failures()
    with patch.object(di.Database, "get_db", return_value=db):
        assert di.create_all_indexes() is True


def test_create_all_indexes_treats_existing_index_as_ok():
    """IndexKeySpecsConflict (code 86) means the index already exists — that
    must not be reported as a failure."""
    from app.services import database_indexes as di

    db = _make_db_with_failures("idx_call_sid_unique")
    with patch.object(di.Database, "get_db", return_value=db):
        assert di.create_all_indexes() is True


def test_create_all_indexes_returns_false_on_real_failure():
    """A non-conflict OperationFailure must surface — not be swallowed."""
    from app.services import database_indexes as di

    failing_collections: dict[str, MagicMock] = {}

    def _make_coll(_name):
        coll = MagicMock()

        def _boom(*a, **k):
            raise OperationFailure("permission denied", code=13, details={})
        coll.create_index.side_effect = _boom
        return coll

    db = MagicMock()
    db.__getitem__.side_effect = lambda n: failing_collections.setdefault(n, _make_coll(n))

    with patch.object(di.Database, "get_db", return_value=db):
        assert di.create_all_indexes() is False
