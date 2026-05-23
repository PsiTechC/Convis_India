"""
Provider Factory — LiveKit stack (Deepgram ASR + OpenAI LLM + ElevenLabs TTS).
"""

import logging
from typing import Optional

from .asr import ASRProvider, DeepgramASR
from .tts import TTSProvider, ElevenLabsTTS

logger = logging.getLogger(__name__)


class ProviderFactory:
    """Factory for creating ASR and TTS providers."""

    ASR_PROVIDERS = {"deepgram": DeepgramASR}
    TTS_PROVIDERS = {"elevenlabs": ElevenLabsTTS}

    @classmethod
    def create_asr_provider(
        cls,
        provider_name: str = "deepgram",
        api_key: Optional[str] = None,
        model: str = "nova-2",
        language: str = "en",
        keywords: Optional[str] = None,
    ) -> ASRProvider:
        if provider_name.lower() != "deepgram":
            raise ValueError(
                f"Unsupported ASR provider '{provider_name}'. Only 'deepgram' is supported."
            )

        logger.info(f"Creating Deepgram ASR with model: {model}")
        return DeepgramASR(api_key=api_key, model=model, language=language, keywords=keywords)

    @classmethod
    def create_tts_provider(
        cls,
        provider_name: str = "elevenlabs",
        api_key: Optional[str] = None,
        voice: str = "rachel",
        **kwargs,
    ) -> TTSProvider:
        if provider_name.lower() != "elevenlabs":
            raise ValueError(
                f"Unsupported TTS provider '{provider_name}'. Only 'elevenlabs' is supported."
            )

        logger.info(f"Creating ElevenLabs TTS with voice: {voice}")
        return ElevenLabsTTS(api_key=api_key, voice=voice)

    @classmethod
    def calculate_cost(
        cls,
        asr_provider: str,
        tts_provider: str,
        duration_minutes: float,
    ) -> dict:
        asr = cls.create_asr_provider(asr_provider)
        tts = cls.create_tts_provider(tts_provider)

        asr_cost = asr.get_cost_per_minute() * duration_minutes
        tts_cost = tts.get_cost_per_minute() * duration_minutes
        llm_cost = 0.10 * duration_minutes  # GPT-4 Turbo approx

        total_cost = asr_cost + tts_cost + llm_cost

        return {
            "asr_cost": round(asr_cost, 4),
            "tts_cost": round(tts_cost, 4),
            "llm_cost": round(llm_cost, 4),
            "total_cost": round(total_cost, 4),
            "cost_per_minute": round(total_cost / duration_minutes, 4) if duration_minutes else 0,
            "duration_minutes": duration_minutes,
        }

    @classmethod
    def get_recommended_combination(cls, priority: str = "balanced") -> dict:
        return {
            "asr_provider": "deepgram",
            "asr_model": "nova-2",
            "tts_provider": "elevenlabs",
            "tts_voice": "rachel",
            "tts_model": "eleven_turbo_v2_5",
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "description": "Deepgram Nova-2 + GPT-4o-mini + ElevenLabs Turbo V2.5",
            "estimated_latency_ms": 300,
            "estimated_cost_per_min": 0.12,
        }
