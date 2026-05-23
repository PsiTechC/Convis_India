"""Diagnose why a freshly-uploaded brochure is giving wrong answers.

Finds the most recently indexed assistant in `knowledge_chunks`, then runs
the exact queries the user asked and shows the top hits + scores.

If the PDF extracted poorly, top hits will be garbled or irrelevant.
If the PDF extracted well but the bot still hallucinated, the agent isn't
calling the tool (system prompt issue or `_has_knowledge_base` cache).
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_ROOT = HERE.parent
sys.path.insert(0, str(API_ROOT))

from dotenv import load_dotenv
load_dotenv(API_ROOT / ".env")

from app.config.database import Database
from app.utils import mongo_rag

OPENAI_KEY = os.getenv("OPENAI_API_KEY")


def main() -> int:
    Database.connect()
    db = Database.get_db()
    coll = db["knowledge_chunks"]

    # Find the most recently uploaded chunk and group by assistant_id
    recent = list(
        coll.find({}, {"assistant_id": 1, "filename": 1, "created_at": 1, "text": 1})
        .sort([("created_at", -1)])
        .limit(50)
    )
    if not recent:
        print("NO chunks in knowledge_chunks at all. Upload didn't reach Mongo.")
        return 1

    # Group by (assistant_id, filename) to find the brochure
    seen = {}
    for c in recent:
        k = (c["assistant_id"], c.get("filename", "?"))
        seen.setdefault(k, []).append(c)
    print("Recently indexed (top 5):")
    for (aid, fn), chunks in list(seen.items())[:5]:
        print(f"  assistant={aid}  file={fn}  chunks_seen={len(chunks)}  newest={chunks[0].get('created_at')}")

    # Pick the most recent assistant_id
    target_aid = recent[0]["assistant_id"]
    target_fn = recent[0].get("filename", "?")
    print(f"\n>>> Diagnosing assistant_id={target_aid} file={target_fn}\n")

    # Total chunks for this assistant
    total = coll.count_documents({"assistant_id": target_aid})
    files = coll.distinct("filename", {"assistant_id": target_aid})
    print(f"Total chunks for this assistant: {total}")
    print(f"Files indexed: {files}")

    # Sample first 3 chunks raw — see if extraction worked
    print("\n--- First 3 chunks (raw text) ---")
    sample = list(coll.find({"assistant_id": target_aid}).sort([("chunk_index", 1)]).limit(3))
    for c in sample:
        snippet = (c.get("text") or "")[:300].replace("\n", " ")
        print(f"  [chunk {c.get('chunk_index')}] ({c.get('char_count')} chars) {snippet}...")

    # Run the user's three test queries
    queries = [
        "how many seats in artificial intelligence and data science",
        "how many books in the library",
        "what is the fee for mechanical engineering",
        "computer engineering intake",
        "principal name",
    ]
    print("\n--- Search results per query (top 3) ---")
    for q in queries:
        print(f"\n  Q: {q!r}")
        try:
            results = mongo_rag.search(target_aid, q, top_k=3, api_key=OPENAI_KEY)
        except Exception as e:
            print(f"    ERROR: {e}")
            continue
        if not results:
            print("    (no results)")
            continue
        for r in results:
            snippet = r["text"][:180].replace("\n", " ")
            print(f"    score={r['score']:.3f}  {snippet}...")
        # What context_for_voice would build
        ctx = mongo_rag.build_context_for_voice(results, min_score=0.45)
        if not ctx:
            print(f"    >>> build_context_for_voice() filtered ALL out (min_score=0.45)")
        else:
            print(f"    >>> context built ({len(ctx)} chars), first 100: {ctx[:100]!r}")

    # Check the assistant doc
    print("\n--- Assistant doc check ---")
    from bson import ObjectId
    try:
        doc = db["assistants"].find_one(
            {"_id": ObjectId(target_aid)},
            {"name": 1, "knowledge_base_files": 1, "system_message": 1},
        )
        if doc:
            kb = doc.get("knowledge_base_files", []) or []
            print(f"  name: {doc.get('name')}")
            print(f"  knowledge_base_files count: {len(kb)}")
            for f in kb:
                print(f"    - {f.get('filename')} ({f.get('chunks_count')} chunks recorded)")
            sm = doc.get("system_message") or ""
            print(f"  system_message length: {len(sm)} chars")
        else:
            print("  ASSISTANT DOC NOT FOUND")
    except Exception as e:
        print(f"  ERROR fetching assistant: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
