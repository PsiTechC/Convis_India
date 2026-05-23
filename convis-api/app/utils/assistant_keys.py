from typing import Tuple, Optional, Dict, Union
from bson import ObjectId
from fastapi import HTTPException, status
import os
import logging

logger = logging.getLogger(__name__)

# Only Deepgram (ASR), OpenAI (LLM), ElevenLabs (TTS) are supported.
SUPPORTED_PROVIDERS = ("deepgram", "openai", "elevenlabs")

ENV_VAR_MAP = {
    "openai": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
}


def _ensure_supported(provider: str) -> str:
    provider = (provider or "").lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider '{provider}' is not supported. Use one of: {', '.join(SUPPORTED_PROVIDERS)}.",
        )
    return provider


def resolve_assistant_api_key(db, assistant: dict, required_provider: Optional[str] = "openai") -> Tuple[str, str]:
    """Retrieve API key from environment variables for a single provider."""
    provider = _ensure_supported(required_provider or "openai")

    env_var = ENV_VAR_MAP[provider]
    api_key = os.getenv(env_var)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"API key not configured for {provider}. Set {env_var} in the environment.",
        )
    return api_key.strip(), provider


def resolve_provider_keys(db, assistant: dict, user_id: ObjectId) -> Dict[str, str]:
    """Resolve API keys for Deepgram/OpenAI/ElevenLabs from environment variables."""
    provider_keys: Dict[str, str] = {}
    for provider, env_var in ENV_VAR_MAP.items():
        value = os.getenv(env_var)
        if value:
            provider_keys[provider] = value.strip()
        else:
            logger.warning(f"⚠ {env_var} not set for provider {provider}")
    return provider_keys


def resolve_user_provider_key(
    db,
    user_id: Union[str, ObjectId],
    provider: str,
    allow_env_fallback: bool = True,
) -> Optional[str]:
    """Retrieve API key from environment for a provider (no DB lookup)."""
    provider = _ensure_supported(provider)
    value = os.getenv(ENV_VAR_MAP[provider])
    return value.strip() if value else None


def resolve_env_provider_keys(
    asr_provider: str,
    tts_provider: str,
    llm_provider: str,
) -> Dict[str, str]:
    """Strict env-only resolution for the LiveKit stack."""
    if asr_provider.lower() != "deepgram":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ASR provider must be 'deepgram'.",
        )
    if tts_provider.lower() != "elevenlabs":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TTS provider must be 'elevenlabs'.",
        )
    if llm_provider.lower() != "openai":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LLM provider must be 'openai'.",
        )

    keys: Dict[str, str] = {}
    missing = []
    for provider, env_var in ENV_VAR_MAP.items():
        value = os.getenv(env_var)
        if value:
            keys[provider] = value.strip()
        else:
            missing.append(provider)

    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing API keys for providers: {', '.join(sorted(missing))}. "
                   "Set them via environment variables.",
        )
    return keys
