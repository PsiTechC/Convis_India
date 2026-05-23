"""Transcriber module — Deepgram only."""
from .base_transcriber import BaseTranscriber
from .deepgram_transcriber import DeepgramTranscriber

__all__ = ["BaseTranscriber", "DeepgramTranscriber"]
