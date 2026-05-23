"""Unit tests for app.services.livekit.assistant_config."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from bson import ObjectId


def _fake_assistant(**overrides):
    doc = {
        "_id": ObjectId(),
        "user_id": ObjectId(),
        "name": "Billing Bot",
        "system_message": "You are a billing assistant.",
        "call_greeting": "Thanks for calling — how can I help?",
        "voice": "alloy",
        "tts_voice": "21m00Tcm4TlvDq8ikWAM",
        "tts_model": "eleven_flash_v2_5",
        "tts_stability": 0.4,
        "tts_similarity_boost": 0.8,
        "tts_style": 0.1,
        "tts_speed": 1.1,
        "asr_model": "nova-2",
        "asr_language": "en",
        "asr_keywords": ["refund", "invoice"],
        "llm_model": "gpt-4o-mini",
        "llm_max_tokens": 250,
        "temperature": 0.6,
        "bot_language": "en",
        "tools_enabled": True,
        "tools": [{"name": "lookup_order"}],
    }
    doc.update(overrides)
    return doc


def _patch_db(db):
    return patch("app.config.database.Database.get_db", return_value=db)


def test_returns_expected_shape(fake_db):
    from app.services.livekit import assistant_config

    assistant = _fake_assistant()
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["calendar_accounts"].find_one.return_value = None

    with _patch_db(fake_db):
        cfg = assistant_config.load_assistant_config(str(assistant["_id"]))

    assert cfg["assistant_id"] == str(assistant["_id"])
    assert cfg["user_id"] == str(assistant["user_id"])
    assert cfg["system_message"].startswith("You are a billing assistant.")
    assert cfg["greeting"] == "Thanks for calling — how can I help?"
    assert cfg["tts_voice"] == "21m00Tcm4TlvDq8ikWAM"
    assert cfg["tts_model"] == "eleven_flash_v2_5"
    assert cfg["asr_model"] == "nova-2"
    assert cfg["asr_keywords"] == ["refund", "invoice"]
    assert cfg["llm_model"] == "gpt-4o-mini"
    assert cfg["llm_max_tokens"] == 250
    assert cfg["temperature"] == 0.6
    assert cfg["calendar_enabled"] is False
    assert cfg["tools_enabled"] is True


def test_missing_assistant_raises_value_error(fake_db):
    from app.services.livekit import assistant_config

    fake_db["assistants"].find_one.return_value = None

    with _patch_db(fake_db), pytest.raises(ValueError, match="Assistant not found"):
        assistant_config.load_assistant_config(str(ObjectId()))


def test_invalid_object_id_raises_value_error(fake_db):
    from app.services.livekit import assistant_config

    # Even with a DB available, an invalid ObjectId should fail up-front.
    with _patch_db(fake_db), pytest.raises(ValueError, match="Invalid assistant_id"):
        assistant_config.load_assistant_config("not-an-object-id")


def test_calendar_enabled_appends_instructions(fake_db):
    from app.services.livekit import assistant_config

    cal_id = ObjectId()
    assistant = _fake_assistant(
        calendar_account_ids=[cal_id],
        calendar_enabled=True,
    )
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["calendar_accounts"].find_one.return_value = {
        "_id": cal_id,
        "user_id": assistant["user_id"],
        "provider": "google",
    }

    with _patch_db(fake_db):
        cfg = assistant_config.load_assistant_config(str(assistant["_id"]))

    assert cfg["calendar_enabled"] is True
    assert str(cal_id) in cfg["calendar_account_ids"]
    assert "Calendar Scheduling Instructions" in cfg["system_message"]


def test_metadata_roundtrip_preserves_payload():
    from app.services.livekit.assistant_config import decode_metadata, encode_metadata

    payload = {
        "assistant_id": "abc",
        "system_message": "hi",
        "nested": [1, 2, 3],
        "obj_id": ObjectId(),
    }
    out = decode_metadata(encode_metadata(payload))
    # ObjectId is stringified by json.dumps(default=str); other fields preserved.
    assert out["assistant_id"] == "abc"
    assert out["nested"] == [1, 2, 3]
    assert out["obj_id"] == str(payload["obj_id"])


# ── Call transfer to a human agent ───────────────────────────────────────────

def test_build_system_message_no_transfer_is_byte_identical():
    """REGRESSION GUARD: build_system_message with call_transfer off (the default)
    must produce the exact same string as before this feature shipped — otherwise
    every existing assistant gets a one-turn OpenAI prompt-cache miss."""
    from app.services.livekit.assistant_config import build_system_message

    kwargs = dict(
        base_message="You are a helpful assistant.",
        calendar_enabled=False,
        timezone_hint="America/New_York",
        expressive_mode=False,
        multilingual=False,
        has_knowledge_base=False,
    )
    # The defaulted form and the explicit-off form must be identical, and must
    # NOT contain the transfer suffix.
    a = build_system_message(**kwargs)
    b = build_system_message(**kwargs, call_transfer_enabled=False, call_transfer_conditions="")
    assert a == b
    assert "TRANSFERRING TO A HUMAN" not in a
    assert "transfer_to_agent" not in a
    # Sanity: it still ends with the end_call suffix (unchanged ordering).
    assert "ENDING THE CALL" in a


def test_build_system_message_with_transfer_appends_suffix():
    from app.services.livekit.assistant_config import build_system_message

    msg = build_system_message(
        base_message="You are a support bot.",
        calendar_enabled=False,
        timezone_hint="America/New_York",
        expressive_mode=False,
        multilingual=False,
        has_knowledge_base=False,
        call_transfer_enabled=True,
        call_transfer_conditions="any refund over $100",
    )
    assert "TRANSFERRING TO A HUMAN" in msg
    assert "transfer_to_agent" in msg
    assert "ADDITIONALLY, transfer when: any refund over $100" in msg
    # End-call instruction is still LAST (transfer slots in before it).
    assert msg.index("TRANSFERRING TO A HUMAN") < msg.index("ENDING THE CALL")
    # No literal placeholder leaked.
    assert "{EXTRA" not in msg and "{conditions}" not in msg


def test_build_system_message_transfer_without_conditions_has_no_extra_bullet():
    from app.services.livekit.assistant_config import build_system_message

    msg = build_system_message(
        base_message="x", calendar_enabled=False, timezone_hint="UTC",
        expressive_mode=False, multilingual=False, has_knowledge_base=False,
        call_transfer_enabled=True, call_transfer_conditions="",
    )
    assert "TRANSFERRING TO A HUMAN" in msg
    assert "ADDITIONALLY" not in msg


def test_coerce_call_transfer_helpers():
    from app.services.livekit import assistant_config as ac
    assert ac._coerce_call_transfer_enabled(True) is True
    assert ac._coerce_call_transfer_enabled("true") is True
    assert ac._coerce_call_transfer_enabled("1") is True
    assert ac._coerce_call_transfer_enabled("nope") is False
    assert ac._coerce_call_transfer_enabled(None) is False
    assert ac._coerce_call_transfer_number("+12025550143") == "+12025550143"
    assert ac._coerce_call_transfer_number("  +12025550143 ") == "+12025550143"
    assert ac._coerce_call_transfer_number("12025550143") == ""   # missing +
    assert ac._coerce_call_transfer_number("+0123") == ""          # leading 0
    assert ac._coerce_call_transfer_number("not a number") == ""
    assert ac._coerce_call_transfer_number(None) == ""
    assert ac._coerce_call_transfer_message("  hi  ") == "hi"
    assert ac._coerce_call_transfer_message("   ") == ""
    assert ac._coerce_call_transfer_conditions("a" * 600) == "a" * 500


def test_load_assistant_config_transfer_enabled_with_valid_number(fake_db):
    from app.services.livekit import assistant_config

    assistant = _fake_assistant(
        call_transfer_enabled=True,
        call_transfer_number="+12025550143",
        call_transfer_message="Hold tight!",
        call_transfer_conditions="billing disputes",
    )
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["calendar_accounts"].find_one.return_value = None

    with _patch_db(fake_db):
        cfg = assistant_config.load_assistant_config(str(assistant["_id"]))

    assert cfg["call_transfer_enabled"] is True
    assert cfg["call_transfer_number"] == "+12025550143"
    assert cfg["call_transfer_message"] == "Hold tight!"
    assert cfg["call_transfer_conditions"] == "billing disputes"
    assert "TRANSFERRING TO A HUMAN" in cfg["system_message"]
    assert "ADDITIONALLY, transfer when: billing disputes" in cfg["system_message"]


def test_load_assistant_config_transfer_enabled_but_bad_number_disables(fake_db):
    """If the toggle is on but the number is junk, the EFFECTIVE flag is False —
    no prompt suffix, and the agent's tool gate is closed."""
    from app.services.livekit import assistant_config

    assistant = _fake_assistant(
        call_transfer_enabled=True,
        call_transfer_number="not-a-number",
    )
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["calendar_accounts"].find_one.return_value = None

    with _patch_db(fake_db):
        cfg = assistant_config.load_assistant_config(str(assistant["_id"]))

    assert cfg["call_transfer_enabled"] is False
    assert cfg["call_transfer_number"] == ""
    assert "TRANSFERRING TO A HUMAN" not in cfg["system_message"]


def test_load_assistant_config_default_transfer_message(fake_db):
    """When transfer is on but no message configured, the resolved message is
    the module default (so the agent's tool always has something to say)."""
    from app.services.livekit import assistant_config

    assistant = _fake_assistant(
        call_transfer_enabled=True, call_transfer_number="+441234567890",
    )
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["calendar_accounts"].find_one.return_value = None

    with _patch_db(fake_db):
        cfg = assistant_config.load_assistant_config(str(assistant["_id"]))

    assert cfg["call_transfer_message"] == assistant_config.DEFAULT_TRANSFER_MESSAGE


def test_load_assistant_config_no_transfer_fields_means_off(fake_db):
    """An assistant doc with no call_transfer_* keys (i.e. every existing
    assistant) → transfer off, no suffix, byte-identical prompt to before."""
    from app.services.livekit import assistant_config
    from app.services.livekit.assistant_config import build_system_message

    assistant = _fake_assistant()  # no call_transfer_* keys
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["calendar_accounts"].find_one.return_value = None

    with _patch_db(fake_db):
        cfg = assistant_config.load_assistant_config(str(assistant["_id"]))

    assert cfg["call_transfer_enabled"] is False
    expected = build_system_message(
        base_message=assistant["system_message"],
        calendar_enabled=False, timezone_hint=cfg["timezone"],
        expressive_mode=False, multilingual=False, has_knowledge_base=False,
    )
    assert cfg["system_message"] == expected


def test_warmer_prompt_matches_load_assistant_config_with_transfer(fake_db):
    """CACHE-CONSISTENCY GUARD: the llm_cache_warmer must build the same
    system_message as the live agent for a transfer-enabled assistant.
    Reproduce the warmer's exact build_system_message inputs and compare to
    load_assistant_config(...)["system_message"]."""
    from app.services.livekit import assistant_config as ac

    assistant = _fake_assistant(
        call_transfer_enabled=True,
        call_transfer_number="+12025550143",
        call_transfer_conditions="any billing dispute",
        # calendar OFF so the warmer's calendar approximation doesn't diverge
        calendar_enabled=False,
    )
    fake_db["assistants"].find_one.return_value = assistant
    fake_db["calendar_accounts"].find_one.return_value = None

    with _patch_db(fake_db):
        cfg = ac.load_assistant_config(str(assistant["_id"]))

    # This mirrors llm_cache_warmer._warm_one's call exactly.
    call_transfer_effective = bool(
        ac._coerce_call_transfer_enabled(assistant.get("call_transfer_enabled"))
        and ac._coerce_call_transfer_number(assistant.get("call_transfer_number"))
    )
    warmer_msg = ac.build_system_message(
        base_message=assistant["system_message"],
        calendar_enabled=bool(assistant.get("calendar_enabled")),
        timezone_hint=assistant.get("timezone") or "America/New_York",
        expressive_mode=ac._coerce_expressive_mode(assistant.get("expressive_mode")),
        multilingual=ac._coerce_multilingual_mode(assistant.get("multilingual")),
        has_knowledge_base=bool(assistant.get("knowledge_base_files")),
        call_transfer_enabled=call_transfer_effective,
        call_transfer_conditions=ac._coerce_call_transfer_conditions(assistant.get("call_transfer_conditions")),
    )
    assert warmer_msg == cfg["system_message"]
