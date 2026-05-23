"""
QA-audit fixtures.

Uses mongomock (a more realistic in-process Mongo than the bespoke
MockMongoCollection in tests/conftest.py) so we can exercise unique indexes,
$ne, $exists, projections, and aggregation behaviour the way the production
code relies on them.

Also provides JWT helpers + a TestClient bound to the mocked DB.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import jwt
import mongomock
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

# Settings refuses to load without these — set before any app.* import.
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/test")
os.environ.setdefault("DATABASE_NAME", "convis_test")
os.environ.setdefault("EMAIL_USER", "test@example.com")
os.environ.setdefault("EMAIL_PASS", "password")
os.environ.setdefault("ENVIRONMENT", "development")
# Disable Twilio signature verification by default for unit tests.
# Tests that specifically exercise the verifier set this to "1" themselves.
os.environ.setdefault("TWILIO_VERIFY_WEBHOOKS", "0")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-twilio-token-not-real")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-do-not-use-in-prod")

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def mongo() -> mongomock.MongoClient:
    """Fresh in-process Mongo for each test."""
    return mongomock.MongoClient()


@pytest.fixture
def db(mongo):
    """Database handle bound to the env DATABASE_NAME."""
    return mongo[os.environ["DATABASE_NAME"]]


@pytest.fixture
def patched_db(db, monkeypatch):
    """Patch Database.get_db() everywhere so route handlers see our mongomock."""
    from app.config import database as db_module
    monkeypatch.setattr(db_module.Database, "get_db", classmethod(lambda cls: db))
    return db


def make_jwt(user_id: str, role: str = "user", expired: bool = False, no_exp: bool = False) -> str:
    """Build a JWT the way login.py does. role values: 'user' or 'admin'."""
    from app.config.settings import settings

    payload = {
        "email": f"user-{user_id}@test.invalid",
        "clientId": user_id,
        "role": role,
    }
    if expired:
        payload["exp"] = datetime.now(timezone.utc) - timedelta(minutes=5)
    elif not no_exp:
        payload["exp"] = datetime.now(timezone.utc) + timedelta(hours=1)
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def auth_headers(user_id: str, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {make_jwt(user_id, role=role)}"}


@pytest.fixture
def make_user(patched_db):
    """Insert a user into the mock DB; return its ObjectId."""
    def _make(*, email: str = "u@test.invalid", role: str = "client", verified: bool = True) -> ObjectId:
        uid = ObjectId()
        patched_db["users"].insert_one({
            "_id": uid,
            "email": email,
            "password": "$2b$12$bogus",
            "role": role,
            "verified": verified,
            "firstLogin": False,
            "companyName": "TestCo",
            "phoneNumber": "+10000000000",
        })
        return uid
    return _make


@pytest.fixture
def make_assistant(patched_db):
    """Insert an assistant into the mock DB; return its ObjectId."""
    def _make(*, user_id: ObjectId, name: str = "Test Bot", system_message: str = "You are helpful.") -> ObjectId:
        aid = ObjectId()
        patched_db["assistants"].insert_one({
            "_id": aid,
            "user_id": user_id,
            "name": name,
            "system_message": system_message,
            "voice": "rachel",
            "temperature": 0.7,
            "llm_model": "gpt-4o-mini",
        })
        return aid
    return _make


@pytest.fixture
def client(patched_db):
    """FastAPI TestClient with the patched mongomock DB."""
    from app.main import app
    with TestClient(app) as c:
        yield c
