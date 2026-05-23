"""
TTS (Text-to-Speech) Provider Abstraction — ElevenLabs only.
"""

import logging
import os
from abc import ABC, abstractmethod
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class TTSProvider(ABC):
    """Base class for TTS providers."""

    def __init__(self, api_key: str, voice: str = "default"):
        self.api_key = api_key
        self.voice = voice
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        pass

    @abstractmethod
    async def synthesize_stream(self, text: str) -> bytes:
        pass

    @abstractmethod
    def get_latency_ms(self) -> int:
        pass

    @abstractmethod
    def get_cost_per_minute(self) -> float:
        pass

    @abstractmethod
    def get_available_voices(self) -> Dict[str, str]:
        pass


class ElevenLabsTTS(TTSProvider):
    """ElevenLabs TTS provider."""

    NATURAL_VOICE_SETTINGS = {
        "stability": 0.71,
        "similarity_boost": 0.75,
        "style": 0.0,
        "use_speaker_boost": True,
    }

    def __init__(self, api_key: Optional[str] = None, voice: str = "rachel"):
        super().__init__(
            api_key=api_key or os.getenv("ELEVENLABS_API_KEY"),
            voice=voice,
        )
        self.client = None
        self._init_client()

    def _init_client(self):
        from elevenlabs import ElevenLabs

        self.client = ElevenLabs(api_key=self.api_key)
        self.logger.info(f"ElevenLabs TTS initialized with voice: {self.voice}")

    async def synthesize(self, text: str) -> bytes:
        audio = self.client.generate(
            text=text,
            voice=self.voice,
            model="eleven_turbo_v2_5",
            output_format="pcm_16000",
            voice_settings=self.NATURAL_VOICE_SETTINGS,
        )
        return b"".join(audio)

    async def synthesize_stream(self, text: str) -> bytes:
        try:
            audio_chunks = []
            for chunk in self.client.generate(
                text=text,
                voice=self.voice,
                model="eleven_turbo_v2_5",
                output_format="pcm_16000",
                stream=True,
                voice_settings=self.NATURAL_VOICE_SETTINGS,
            ):
                audio_chunks.append(chunk)
            return b"".join(audio_chunks)
        except Exception as e:
            self.logger.error(f"ElevenLabs streaming error: {e}")
            return await self.synthesize(text)

    def get_latency_ms(self) -> int:
        return 150

    def get_cost_per_minute(self) -> float:
        return 0.018

    def get_available_voices(self) -> Dict[str, str]:
        return {
            "rachel": "Young female American voice",
            "domi": "Strong female American voice",
            "bella": "Soft young American female",
            "antoni": "Well-rounded male voice",
            "josh": "Deep American male voice",
            "arnold": "Crisp American male",
        }
