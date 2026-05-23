"""Settings security regression tests.

Bug guarded: settings used to default jwt_secret to a constant, repo-known
string, and used to print os.environ.keys() to stdout on import failure.
"""
from __future__ import annotations

import importlib

import pytest


def test_settings_class_has_no_jwt_secret_default():
    """Pydantic field for jwt_secret must be required (no default)."""
    from app.config.settings import Settings

    field = Settings.model_fields["jwt_secret"]
    assert field.is_required(), (
        "jwt_secret must have NO default — leaking the default into deploys is "
        "the entire bug we're guarding against"
    )


def test_settings_module_does_not_log_environ_keys():
    """The fatal-startup error path must not log env var names."""
    import inspect

    import app.config.settings as settings_mod
    source = inspect.getsource(settings_mod)
    assert "os.environ.keys()" not in source, (
        "Settings module must not leak environment variable names on startup failure"
    )
    assert "list(os.environ" not in source, (
        "Settings module must not dump os.environ on startup failure"
    )
