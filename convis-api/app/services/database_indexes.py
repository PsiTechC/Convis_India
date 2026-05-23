"""
Database Indexes Setup for Convis.

Each desired index is defined declaratively. Per-index errors are tolerated
(IndexKeySpecsConflict means the index already exists with compatible-enough
spec — we log + skip), but every other failure is recorded and surfaced so the
caller does NOT see "all created" on a partial run.
"""
from typing import List, Sequence, Tuple, Union

from pymongo.errors import OperationFailure

from app.config.database import Database
from app.voice_pipeline.helpers.logger_config import configure_logger

logger = configure_logger(__name__)

# IndexSpec: (collection, keys, name, kwargs)
IndexKey = Union[str, Sequence[Tuple[str, int]]]
IndexSpec = Tuple[str, IndexKey, str, dict]

INDEX_SPECS: List[IndexSpec] = [
    # call_logs
    ("call_logs", "call_sid", "idx_call_sid_unique", {"unique": True}),
    ("call_logs", [("user_id", 1), ("created_at", -1)], "idx_user_calls", {}),
    ("call_logs", "status", "idx_status", {}),
    ("call_logs", [("created_at", -1)], "idx_created_at", {}),
    # ai_assistants
    ("ai_assistants", "user_id", "idx_assistant_user", {}),
    ("ai_assistants", [("user_id", 1), ("created_at", -1)], "idx_assistant_user_created", {}),
    # phone_numbers
    ("phone_numbers", "phone_number", "idx_phone_unique", {"unique": True}),
    ("phone_numbers", "user_id", "idx_phone_user", {}),
    # provider_connections
    ("provider_connections", [("user_id", 1), ("provider", 1)], "idx_provider_user", {}),
    # users
    ("users", "email", "idx_user_email_unique", {"unique": True}),
    # campaigns
    ("campaigns", "user_id", "idx_campaign_user", {}),
    (
        "campaigns",
        [("user_id", 1), ("status", 1), ("scheduled_time", 1)],
        "idx_campaign_active",
        {},
    ),
    # leads
    ("leads", [("campaign_id", 1), ("status", 1), ("_id", 1)], "idx_leads_campaign_status_id", {}),
    ("leads", [("campaign_id", 1), ("retry_on", 1)], "idx_leads_campaign_retry_on", {}),
    ("leads", [("campaign_id", 1), ("order_index", 1)], "idx_leads_campaign_order", {}),
    # ── Public demo-call (rate-limit + abuse forensics) ──
    # 30-day TTL — rate-limit only ever queries last 24h, but we keep a
    # month for after-the-fact abuse investigation.
    ("demo_call_attempts", "created_at", "idx_demo_call_attempts_ttl", {"expireAfterSeconds": 30*24*3600}),
    ("demo_call_attempts", [("ip", 1), ("created_at", -1)], "idx_demo_call_attempts_by_ip", {}),
    ("demo_call_attempts", [("to_number", 1), ("created_at", -1)], "idx_demo_call_attempts_by_number", {}),
    # 7-day TTL — OTP-send records have a much shorter useful life.
    ("demo_otp_requests", "created_at", "idx_demo_otp_requests_ttl", {"expireAfterSeconds": 7*24*3600}),
    ("demo_otp_requests", [("ip", 1), ("created_at", -1)], "idx_demo_otp_requests_by_ip", {}),
    ("demo_otp_requests", [("to_number", 1), ("created_at", -1)], "idx_demo_otp_requests_by_number", {}),
    # ── Conversation memory across calls (P1+P2 feature) ──
    # Documented inline in app/models/contact.py and app/models/call_summary.py.
    # The unique indexes are load-bearing: they're what makes the upsert paths
    # in contact_service and post_call_summary_service idempotent under
    # concurrent Twilio webhook retries.
    (
        "contacts",
        [("user_id", 1), ("phone_number", 1)],
        "uniq_user_phone",
        {"unique": True},
    ),
    ("contacts", [("user_id", 1), ("updated_at", -1)], "idx_contacts_recent_per_user", {}),
    (
        "call_summaries",
        [("contact_id", 1), ("date", -1)],
        "idx_summaries_recent_per_contact",
        {},
    ),
    (
        "call_summaries",
        [("call_log_id", 1)],
        "uniq_call_log_summary",
        {"unique": True},
    ),
    ("call_summaries", [("user_id", 1), ("date", -1)], "idx_summaries_recent_per_user", {}),
]


def _create_one(db, spec: IndexSpec) -> Tuple[bool, str]:
    """Returns (created_or_skipped_ok, message). Does NOT raise."""
    collection_name, keys, name, kwargs = spec
    coll = db[collection_name]
    try:
        coll.create_index(keys, name=name, **kwargs)
        return True, f"created {collection_name}.{name}"
    except OperationFailure as exc:
        # 85 = IndexOptionsConflict, 86 = IndexKeySpecsConflict
        # Both mean an index with this name already exists. We treat it as OK
        # but log loudly so an operator can reconcile if needed.
        if exc.code in (85, 86):
            logger.warning(
                "[DATABASE_INDEXES] %s.%s already exists with a different spec; "
                "leaving as-is. (mongo code=%s) %s",
                collection_name, name, exc.code, exc.details,
            )
            return True, f"skipped existing {collection_name}.{name}"
        logger.error(
            "[DATABASE_INDEXES] Unexpected OperationFailure on %s.%s: %s",
            collection_name, name, exc, exc_info=True,
        )
        return False, f"FAILED {collection_name}.{name}: {exc}"
    except Exception as exc:
        logger.error(
            "[DATABASE_INDEXES] Unexpected error on %s.%s: %s",
            collection_name, name, exc, exc_info=True,
        )
        return False, f"FAILED {collection_name}.{name}: {exc}"


def create_all_indexes() -> bool:
    """Create or verify every index in INDEX_SPECS.

    Returns True only if every spec succeeded (or was harmless to skip).
    On partial failure, returns False — caller MUST surface this rather than
    logging '✅ created/verified' over the top.
    """
    db = Database.get_db()
    logger.info("[DATABASE_INDEXES] Starting index creation/verification...")

    failures: List[str] = []
    for spec in INDEX_SPECS:
        ok, msg = _create_one(db, spec)
        if ok:
            logger.info("[DATABASE_INDEXES] ✅ %s", msg)
        else:
            failures.append(msg)

    if failures:
        logger.error(
            "[DATABASE_INDEXES] ❌ Index setup completed with %d failure(s):\n  - %s",
            len(failures), "\n  - ".join(failures),
        )
        return False

    logger.info("[DATABASE_INDEXES] 🎉 All indexes created/verified")
    return True


def list_all_indexes() -> bool:
    try:
        db = Database.get_db()
        collections = sorted({spec[0] for spec in INDEX_SPECS})
        logger.info("[DATABASE_INDEXES] Current indexes:")
        for collection_name in collections:
            indexes = db[collection_name].list_indexes()
            logger.info("\n%s:", collection_name)
            for idx in indexes:
                logger.info("  - %s: %s", idx["name"], idx.get("key", {}))
        return True
    except Exception as e:
        logger.error("[DATABASE_INDEXES] Failed to list indexes: %s", e, exc_info=True)
        return False


if __name__ == "__main__":
    ok = create_all_indexes()
    list_all_indexes()
    raise SystemExit(0 if ok else 1)
