"""Local smoke test for the new mongo_rag pipeline.

Runs against the REAL MongoDB defined in convis-api/.env. Uses a synthetic
assistant_id so it can't collide with a real customer's KB. Cleans up after
itself.

What it verifies:
  1. _ensure_indexes() is idempotent and doesn't crash
  2. index_document() chunks + embeds + writes to knowledge_chunks
  3. kb_stats() reflects the inserted chunks
  4. search() returns results, top hit text contains an expected substring
  5. build_context_for_voice() produces a non-empty bullet list
  6. delete_document() actually removes the chunks
  7. LRU cache eviction works at the boundary

Exits 0 on success, 1 on any failure.

Run from repo root with:
    /Users/psitech/miniconda3/envs/gen/bin/python convis-api/scripts/smoke_test_rag.py
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

# Set repo paths + load .env BEFORE importing app modules
HERE = Path(__file__).resolve().parent
API_ROOT = HERE.parent
REPO_ROOT = API_ROOT.parent
sys.path.insert(0, str(API_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(API_ROOT / ".env")

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY:
    print("FAIL: OPENAI_API_KEY not set in .env — needed for embeddings")
    sys.exit(1)


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def passed(msg: str) -> None:
    print(f"  {_green('PASS')}  {msg}")


def failed(msg: str) -> str:
    print(f"  {_red('FAIL')}  {msg}")
    raise AssertionError(msg)


def main() -> int:
    print("\n[smoke] Importing app modules...")
    from app.utils import mongo_rag  # noqa: E402
    from app.config.database import Database  # noqa: E402

    test_assistant_id = f"smoke-test-{uuid.uuid4().hex[:8]}"
    test_filename = "smoke_test_doc.txt"
    print(f"[smoke] Test assistant_id: {test_assistant_id}")

    # Ensure DB connection works
    Database.connect()
    print("[smoke] Mongo connected")

    # 1. Indexes
    print("\n[1] _ensure_indexes()")
    mongo_rag._ensure_indexes()
    passed("indexes ensured (idempotent)")

    # 2. index_document
    print("\n[2] index_document()")
    test_text = (
        "Convis is an AI voice agent platform built by Psitech. "
        "It uses LiveKit Cloud for SIP integration with Twilio for PSTN. "
        "Speech-to-text is handled by Deepgram with the nova-2-phonecall model. "
        "The language model is OpenAI gpt-4o-mini for low latency. "
        "Text-to-speech defaults to ElevenLabs Flash v2.5, with Cartesia Sonic as a cheaper alternative. "
        "\n\nFor pricing questions: the standard plan costs $99 per month and includes 500 minutes of calls. "
        "The pro plan is $299 per month with 2000 minutes plus priority support. "
        "Enterprise plans start at $999 per month with unlimited minutes and a dedicated success manager. "
        "\n\nRefund policy: customers can request a refund within 30 days of purchase. "
        "Refunds are processed within 5 business days back to the original payment method."
    )
    result = mongo_rag.index_document(
        assistant_id=test_assistant_id,
        text=test_text,
        filename=test_filename,
        file_type="txt",
        api_key=OPENAI_KEY,
    )
    if not result.get("success"):
        failed(f"index_document returned {result}")
    if result["chunks_count"] < 2:
        failed(f"expected >= 2 chunks, got {result['chunks_count']}")
    passed(f"indexed {result['chunks_count']} chunks")

    # 3. kb_stats
    print("\n[3] kb_stats()")
    stats = mongo_rag.kb_stats(test_assistant_id)
    if stats["chunks_count"] != result["chunks_count"]:
        failed(f"stats chunks_count={stats['chunks_count']} != {result['chunks_count']}")
    if stats["files_count"] != 1:
        failed(f"expected files_count=1, got {stats['files_count']}")
    passed(f"stats reflect insertion: {stats}")

    # 4. search — pricing query
    print("\n[4] search() — pricing query")
    hits = mongo_rag.search(
        assistant_id=test_assistant_id,
        query="how much does the pro plan cost",
        top_k=3,
        api_key=OPENAI_KEY,
    )
    if not hits:
        failed("search returned no hits for an obvious match")
    top = hits[0]
    if "pro" not in top["text"].lower() and "299" not in top["text"]:
        failed(f"top hit doesn't mention 'pro' or '299': {top['text'][:200]}")
    if top["score"] < 0.4:
        failed(f"top score too low: {top['score']:.3f}")
    passed(f"top hit score={top['score']:.3f}, text snippet=\"{top['text'][:80]}...\"")

    # 5. search — refund query
    print("\n[5] search() — refund query")
    hits = mongo_rag.search(
        assistant_id=test_assistant_id,
        query="can I get a refund",
        top_k=3,
        api_key=OPENAI_KEY,
    )
    if not hits or "refund" not in hits[0]["text"].lower():
        failed(f"refund query failed to find refund text. Top hit: {hits[0] if hits else None}")
    passed(f"refund top hit score={hits[0]['score']:.3f}")

    # 6. build_context_for_voice
    print("\n[6] build_context_for_voice()")
    ctx = mongo_rag.build_context_for_voice(hits, min_score=0.45, max_chars=2400)
    if not ctx:
        failed("expected non-empty context for relevant query")
    if "documentation" not in ctx.lower():
        failed("context missing the 'official documentation' header")
    passed(f"context built ({len(ctx)} chars)")

    # 7. search — irrelevant query
    print("\n[7] search() — irrelevant query")
    hits = mongo_rag.search(
        assistant_id=test_assistant_id,
        query="recipe for chocolate chip cookies",
        top_k=3,
        api_key=OPENAI_KEY,
    )
    # Hits will exist (cosine never returns zero) but build_context_for_voice
    # should filter them out.
    ctx = mongo_rag.build_context_for_voice(hits, min_score=0.45)
    if ctx:
        # Not necessarily a fail — but worth flagging
        print(f"  WARN  irrelevant query produced context (top score={hits[0]['score']:.3f}). Consider raising min_score.")
    else:
        passed(f"irrelevant query filtered out (top score={hits[0]['score']:.3f})")

    # 8. LRU cache eviction
    print("\n[8] LRU cache eviction")
    # Force the cache near capacity then verify oldest gets evicted
    original_max = mongo_rag._CACHE_MAX_ENTRIES
    mongo_rag._CACHE_MAX_ENTRIES = 3
    mongo_rag._chunk_cache.clear()
    # Insert 4 fake entries — the first should be evicted
    from datetime import datetime, timezone
    for i in range(4):
        mongo_rag._store_in_cache(f"fake-{i}", {
            "chunks": [], "matrix": None,
            "loaded_at": datetime.now(timezone.utc),
        })
    if "fake-0" in mongo_rag._chunk_cache:
        failed("oldest entry should have been evicted")
    if len(mongo_rag._chunk_cache) != 3:
        failed(f"cache size should be 3, got {len(mongo_rag._chunk_cache)}")
    passed(f"LRU evicted oldest, cache size = {len(mongo_rag._chunk_cache)}")
    mongo_rag._CACHE_MAX_ENTRIES = original_max

    # 9. delete_document
    print("\n[9] delete_document()")
    deleted = mongo_rag.delete_document(test_assistant_id, test_filename)
    if deleted != result["chunks_count"]:
        failed(f"deleted {deleted} != indexed {result['chunks_count']}")
    stats_after = mongo_rag.kb_stats(test_assistant_id)
    if stats_after["chunks_count"] != 0:
        failed(f"after delete, expected 0 chunks, got {stats_after['chunks_count']}")
    passed(f"deleted {deleted} chunks; stats now {stats_after}")

    print("\n" + _green("ALL SMOKE TESTS PASSED"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError:
        print(_red("\nSMOKE FAILED — see above"))
        sys.exit(1)
    except Exception as e:
        import traceback
        print(_red(f"\nSMOKE CRASHED: {e}"))
        traceback.print_exc()
        sys.exit(1)
