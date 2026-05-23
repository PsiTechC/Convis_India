"""
ASR (Automatic Speech Recognition) Provider Abstraction — Deepgram only.
"""

import logging
import os
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)


class ASRProvider(ABC):
    """Base class for ASR providers."""

    def __init__(self, api_key: str, model: str = "default", language: str = "en"):
        self.api_key = api_key
        self.model = model
        self.language = language
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    async def transcribe_stream(self, audio_stream: AsyncIterator[bytes]) -> AsyncIterator[str]:
        pass

    @abstractmethod
    async def transcribe(self, audio_bytes: bytes) -> str:
        pass

    @abstractmethod
    def get_latency_ms(self) -> int:
        pass

    @abstractmethod
    def get_cost_per_minute(self) -> float:
        pass


class DeepgramASR(ASRProvider):
    """Deepgram Nova-2 ASR provider."""

    DEFAULT_KEYWORDS = [
        "gmail:100", "yahoo:100", "outlook:100", "hotmail:100",
        "icloud:80", "protonmail:80", "aol:80",
        "@:50", "dot com:80", "dot in:80", "dot org:80", "dot net:80",
        "at the rate:50", "at sign:50",
    ]

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "nova-2",
        language: str = "en",
        keywords: Optional[str] = None,
    ):
        super().__init__(
            api_key=api_key or os.getenv("DEEPGRAM_API_KEY"),
            model=model,
            language=language,
        )
        self.deepgram = None
        self.keywords = self._build_keywords(keywords)
        self._init_client()

    def _build_keywords(self, user_keywords: Optional[str]) -> Optional[str]:
        user_kw_list = []
        if user_keywords:
            user_kw_list = [kw.strip() for kw in user_keywords.split(",") if kw.strip()]

        all_keywords = list(user_kw_list)
        for default_kw in self.DEFAULT_KEYWORDS:
            kw_base = default_kw.split(":")[0].lower()
            if not any(kw_base in ukw.lower() for ukw in all_keywords):
                all_keywords.append(default_kw)

        return ",".join(all_keywords) if all_keywords else None

    def _init_client(self):
        from deepgram import DeepgramClient

        self.deepgram = DeepgramClient(api_key=self.api_key)
        self.logger.info(f"Deepgram ASR initialized with model: {self.model}")

    async def transcribe_stream(self, audio_stream: AsyncIterator[bytes]) -> AsyncIterator[str]:
        options = {
            "punctuate": True,
            "model": self.model,
            "language": self.language,
            "encoding": "linear16",
            "sample_rate": 8000,
            "channels": 1,
            "interim_results": False,
            "endpointing": 300,
        }
        if self.keywords:
            options["keywords"] = self.keywords

        deepgramLive = await self.deepgram.transcription.live(options)

        async def handle_transcript(transcript):
            if transcript:
                text = (
                    transcript.get("channel", {})
                    .get("alternatives", [{}])[0]
                    .get("transcript", "")
                )
                if text:
                    yield text

        deepgramLive.registerHandler(
            deepgramLive.event.TRANSCRIPT_RECEIVED,
            handle_transcript,
        )

        async for audio_chunk in audio_stream:
            deepgramLive.send(audio_chunk)

        deepgramLive.finish()

    async def transcribe(self, audio_bytes: bytes) -> str:
        from deepgram import PrerecordedOptions, FileSource

        options_dict = {
            "model": self.model,
            "punctuate": True,
            "language": self.language,
        }
        if self.keywords:
            options_dict["keywords"] = self.keywords.split(",")

        options = PrerecordedOptions(**options_dict)
        payload = FileSource(buffer=audio_bytes)

        response = await self.deepgram.listen.asyncrest.v("1").transcribe_file(payload, options)
        return response["results"]["channels"][0]["alternatives"][0]["transcript"]

    def get_latency_ms(self) -> int:
        return 75

    def get_cost_per_minute(self) -> float:
        return 0.0043
