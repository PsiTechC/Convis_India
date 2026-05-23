"""MongoDB-backed RAG for the voice agent.

Replaces the previous ChromaDB-on-local-disk implementation, which broke in
production because each ECS Fargate task had its own ephemeral filesystem
(documents uploaded to the API task were invisible to the agent task).

Stores chunks + embeddings in a single MongoDB collection `knowledge_chunks`
scoped by assistant_id. At query time, loads all chunks for that assistant
into a numpy matrix once per worker process and runs cosine similarity in
memory. Performance budget:
  - Up to ~10K chunks per assistant: <50 ms search latency
  - Beyond that: switch to a real vector DB (Qdrant / pgvector)

For a 1000-page document at chunk_size=300 tokens with overlap=50,
expect ~3000-4000 chunks ≈ 24 MB of embeddings on disk and ~10 ms search.

Schema (one doc per chunk):
  {
    _id, assistant_id, filename, chunk_index, text,
    embedding: [1536 floats],   # text-embedding-3-small dim
    paragraph_id, char_count, file_type, created_at
  }
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np

from app.config.database import Database
from app.utils.local_embeddings import (
    EmbeddingProviderError,
    create_embeddings_auto,
)

logger = logging.getLogger(__name__)


# In-memory cache of chunks per assistant. Bounded LRU so a worker process
# serving many distinct assistants over its lifetime can't grow unbounded —
# each cached entry can hold tens of MB (1536-d embeddings × thousands of
# chunks). Eviction policy: when full, drop the least-recently-used entry.
# Stale entries (older than TTL) are also dropped on access. Each ECS task
# has its own cache; cross-task cache busting happens via the
# `assistants.kb_last_modified_at` timestamp on upload/delete (not yet wired —
# until then, expect up to TTL seconds of staleness across tasks).
_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 32
_chunk_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def _ensure_indexes() -> None:
    """Create the indexes needed for fast per-assistant queries. Idempotent —
    safe to call on every startup. Run once via the API's startup hook.
    """
    db = Database.get_db()
    db["knowledge_chunks"].create_index([("assistant_id", 1)])
    db["knowledge_chunks"].create_index([("assistant_id", 1), ("filename", 1)])


def _bust_cache(assistant_id: str) -> None:
    _chunk_cache.pop(assistant_id, None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def index_document(
    assistant_id: str,
    text: str,
    filename: str,
    file_type: str,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Chunk + embed `text` and store the chunks in MongoDB scoped to this
    assistant. Replaces any existing chunks for the same (assistant, filename).

    Returns:
      {"success": bool, "chunks_count": int, "error": str?}
    """
    if not text or not text.strip():
        return {"success": False, "error": "Empty document text"}

    # Reuse the existing chunker (300-token chunks, 50-token overlap) so the
    # behavior matches what knowledge_base upload already produces.
    from app.utils.conversational_rag import chunk_text_for_conversation
    chunks = chunk_text_for_conversation(text, chunk_size=300, overlap=50)
    if not chunks:
        return {"success": False, "error": "No chunks created from document"}

    chunk_texts = [c["text"] for c in chunks]
    try:
        embeddings = create_embeddings_auto(chunk_texts, api_key)
    except EmbeddingProviderError as e:
        return {"success": False, "error": f"Embedding provider failed: {e}"}
    if len(embeddings) != len(chunk_texts):
        return {"success": False, "error": "Embedding count mismatch"}

    db = Database.get_db()
    coll = db["knowledge_chunks"]
    # Replace any existing chunks for this (assistant, filename) so re-uploads
    # don't duplicate.
    coll.delete_many({"assistant_id": assistant_id, "filename": filename})
    docs = [
        {
            "assistant_id": assistant_id,
            "filename": filename,
            "chunk_index": i,
            "text": chunks[i]["text"],
            "embedding": embeddings[i],
            "paragraph_id": chunks[i].get("paragraph_id"),
            "char_count": chunks[i].get("char_count"),
            "file_type": file_type,
            "created_at": _now(),
        }
        for i in range(len(chunks))
    ]
    coll.insert_many(docs)
    _bust_cache(assistant_id)

    logger.info(
        "[RAG] Indexed %s for assistant=%s: %d chunks",
        filename, assistant_id, len(docs),
    )
    return {"success": True, "chunks_count": len(docs)}


def delete_document(assistant_id: str, filename: str) -> int:
    """Remove all chunks for a single (assistant, filename). Returns the
    number of chunks deleted."""
    db = Database.get_db()
    res = db["knowledge_chunks"].delete_many(
        {"assistant_id": assistant_id, "filename": filename}
    )
    _bust_cache(assistant_id)
    return res.deleted_count


def kb_stats(assistant_id: str) -> Dict[str, Any]:
    """Aggregate count of chunks + distinct files for this assistant."""
    db = Database.get_db()
    chunks = db["knowledge_chunks"].count_documents({"assistant_id": assistant_id})
    files = len(
        db["knowledge_chunks"].distinct("filename", {"assistant_id": assistant_id})
    )
    return {"chunks_count": chunks, "files_count": files}


def _store_in_cache(assistant_id: str, cache: Dict[str, Any]) -> None:
    """Insert into the LRU, evicting the oldest entry when over capacity."""
    if assistant_id in _chunk_cache:
        _chunk_cache.move_to_end(assistant_id)
    _chunk_cache[assistant_id] = cache
    while len(_chunk_cache) > _CACHE_MAX_ENTRIES:
        _chunk_cache.popitem(last=False)


def _load_chunks(assistant_id: str) -> Dict[str, Any]:
    """Load and pre-process all chunks for an assistant into memory. The
    embedding matrix is L2-normalized once so cosine similarity at query
    time is just a dot product (a single matmul).

    Cached per-process in a bounded LRU for _CACHE_TTL_SECONDS. Cache is
    busted whenever `index_document` or `delete_document` runs in the same
    process. Other processes pick up changes after the TTL.
    """
    cached = _chunk_cache.get(assistant_id)
    if cached is not None:
        age = (_now() - cached["loaded_at"]).total_seconds()
        if age < _CACHE_TTL_SECONDS:
            _chunk_cache.move_to_end(assistant_id)  # mark as recently used
            return cached

    db = Database.get_db()
    raw = list(
        db["knowledge_chunks"].find(
            {"assistant_id": assistant_id},
            {"text": 1, "embedding": 1, "filename": 1, "chunk_index": 1},
        )
    )

    if not raw:
        cache = {"chunks": [], "matrix": None, "loaded_at": _now()}
        _store_in_cache(assistant_id, cache)
        return cache

    matrix = np.asarray([c["embedding"] for c in raw], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix = matrix / norms

    cache = {"chunks": raw, "matrix": matrix, "loaded_at": _now()}
    _store_in_cache(assistant_id, cache)
    logger.info(
        "[RAG] Loaded %d chunks for assistant=%s into memory (cache size=%d)",
        len(raw), assistant_id, len(_chunk_cache),
    )
    return cache


def search(
    assistant_id: str,
    query: str,
    top_k: int = 5,
    api_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return the top-K most relevant chunks for `query`.
    Each result: {text, filename, score, chunk_index}. score in [-1, 1].
    Empty list if the assistant has no documents indexed or embedding fails.
    """
    cache = _load_chunks(assistant_id)
    chunks = cache["chunks"]
    matrix = cache["matrix"]
    if not chunks or matrix is None:
        return []

    if not query or not query.strip():
        return []

    try:
        q_emb = create_embeddings_auto([query], api_key)
    except EmbeddingProviderError as e:
        logger.warning("[RAG] Query embedding failed: %s", e)
        return []
    if not q_emb:
        return []

    q_vec = np.asarray(q_emb[0], dtype=np.float32)
    q_norm = np.linalg.norm(q_vec)
    if q_norm == 0:
        return []
    q_vec = q_vec / q_norm

    scores = matrix @ q_vec  # cosine sim = dot of normalized vectors
    top_idx = np.argsort(-scores)[: max(1, top_k)]

    return [
        {
            "text": chunks[i]["text"],
            "filename": chunks[i]["filename"],
            "score": float(scores[i]),
            "chunk_index": chunks[i].get("chunk_index", -1),
        }
        for i in top_idx
    ]


def build_context_for_voice(
    results: List[Dict[str, Any]],
    min_score: float = 0.45,
    max_chars: int = 2400,
) -> str:
    """Format top-K results into a context string the LLM can use.

    Below `min_score`, results are usually noise. We also cap total
    characters so we don't blow up the LLM prompt and tank cache hits.
    """
    relevant = [r for r in results if r["score"] >= min_score]
    if not relevant:
        return ""
    parts: List[str] = ["Relevant excerpts from the official documentation:"]
    used = len(parts[0])
    for r in relevant:
        snippet = r["text"].strip()
        if used + len(snippet) + 4 > max_chars:
            break
        parts.append(f"- {snippet}")
        used += len(snippet) + 4
    return "\n".join(parts)
