"""
Embeddings utility.

Exposes `create_embeddings_auto(texts, api_key=None)` which selects the
embedding backend based on the EMBEDDING_PROVIDER env var. Defaults to
OpenAI (text-embedding-3-small) to match the cloud-only deployment.

Set EMBEDDING_PROVIDER=local to use a local SentenceTransformers model
(requires sentence-transformers installed); otherwise OpenAI is used.
"""
import os
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
LOCAL_EMBEDDING_MODEL = os.getenv("LOCAL_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

_local_model = None


def _get_local_model():
    global _local_model
    if _local_model is None:
        from sentence_transformers import SentenceTransformer
        logger.info(f"[EMBEDDINGS] Loading local model {LOCAL_EMBEDDING_MODEL}")
        _local_model = SentenceTransformer(LOCAL_EMBEDDING_MODEL)
    return _local_model


def _create_embeddings_openai(texts: List[str], api_key: Optional[str]) -> List[List[float]]:
    from openai import OpenAI

    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        raise ValueError("OpenAI API key is required for embeddings")

    client = OpenAI(api_key=key)
    response = client.embeddings.create(model=OPENAI_EMBEDDING_MODEL, input=texts)
    return [item.embedding for item in response.data]


def _create_embeddings_local(texts: List[str]) -> List[List[float]]:
    model = _get_local_model()
    vectors = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
    return [v.tolist() for v in vectors]


class EmbeddingProviderError(RuntimeError):
    """Raised when the embedding provider fails to produce vectors."""


def create_embeddings_auto(texts: List[str], api_key: Optional[str] = None) -> List[List[float]]:
    """Create embeddings for a list of texts.

    Selects provider based on EMBEDDING_PROVIDER env var. Returns [] only when
    the input was empty. Any provider failure raises EmbeddingProviderError —
    callers must distinguish "no input" from "API failure".
    """
    if not texts:
        return []

    provider = os.getenv("EMBEDDING_PROVIDER", "openai").lower()

    try:
        if provider == "local":
            return _create_embeddings_local(texts)
        return _create_embeddings_openai(texts, api_key)
    except EmbeddingProviderError:
        raise
    except Exception as e:
        logger.error(f"[EMBEDDINGS] {provider} embeddings failed: {e}", exc_info=True)
        raise EmbeddingProviderError(
            f"Failed to create embeddings via {provider}: {e}"
        ) from e
