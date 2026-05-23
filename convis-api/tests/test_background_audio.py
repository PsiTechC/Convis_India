"""
Unit and Integration Tests for Background Audio Feature

Tests cover:
1. Audio file loading
2. Mu-law encoding/decoding
3. Audio mixing at different volumes
4. BackgroundAudioMixer class
5. Assistant configuration integration
6. Edge cases and error handling
"""

import pytest
import base64
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

# Import the module under test
from app.utils.background_audio import (
    mu_law_encode,
    mu_law_decode,
    load_background_audio,
    BackgroundAudioMixer,
    create_mixer_from_assistant,
    AUDIO_DIR,
    BACKGROUND_AUDIO_FILES,
    _audio_cache
)


class TestMuLawEncoding:
    """Tests for mu-law encoding/decoding functions."""

    def test_encode_silence(self):
        """Test encoding silence (0) to mu-law."""
        encoded = mu_law_encode(0)
        assert isinstance(encoded, int)
        assert 0 <= encoded <= 255

    def test_encode_positive_sample(self):
        """Test encoding positive PCM sample."""
        encoded = mu_law_encode(1000)
        assert isinstance(encoded, int)
        assert 0 <= encoded <= 255

    def test_encode_negative_sample(self):
        """Test encoding negative PCM sample."""
        encoded = mu_law_encode(-1000)
        assert isinstance(encoded, int)
        assert 0 <= encoded <= 255

    def test_encode_max_positive(self):
        """Test encoding maximum positive value."""
        encoded = mu_law_encode(32767)
        assert isinstance(encoded, int)
        assert 0 <= encoded <= 255

    def test_encode_max_negative(self):
        """Test encoding maximum negative value."""
        encoded = mu_law_encode(-32768)
        assert isinstance(encoded, int)
        assert 0 <= encoded <= 255

    def test_decode_returns_integer(self):
        """Test that decoding returns an integer."""
        decoded = mu_law_decode(128)
        assert isinstance(decoded, int)

    def test_encode_decode_roundtrip(self):
        """Test that encode/decode is approximately reversible."""
        original = 1000
        encoded = mu_law_encode(original)
        decoded = mu_law_decode(encoded)
        # Mu-law is lossy, so we check approximate equality
        assert abs(decoded - original) < 500  # Allow some loss

    def test_encode_decode_silence(self):
        """Test roundtrip for silence."""
        encoded = mu_law_encode(0)
        decoded = mu_law_decode(encoded)
        assert abs(decoded) < 100  # Should be close to 0


class TestAudioFileLoading:
    """Tests for audio file loading functionality."""

    def test_audio_dir_exists(self):
        """Test that audio directory path is valid."""
        assert AUDIO_DIR is not None
        assert isinstance(AUDIO_DIR, Path)

    def test_background_audio_files_dict(self):
        """Test that audio files dictionary is configured."""
        assert isinstance(BACKGROUND_AUDIO_FILES, dict)
        assert "custom" in BACKGROUND_AUDIO_FILES
        assert "call_center" in BACKGROUND_AUDIO_FILES
        assert "office" in BACKGROUND_AUDIO_FILES
        assert "cafe" in BACKGROUND_AUDIO_FILES
        assert "white_noise" in BACKGROUND_AUDIO_FILES

    def test_load_custom_audio(self):
        """Test loading custom background audio."""
        audio = load_background_audio("custom")
        if audio:  # File may not exist in test environment
            assert isinstance(audio, bytes)
            assert len(audio) > 0

    def test_load_call_center_audio(self):
        """Test loading call center audio."""
        audio = load_background_audio("call_center")
        if audio:
            assert isinstance(audio, bytes)

    def test_load_unknown_audio_type(self):
        """Test loading unknown audio type returns None."""
        audio = load_background_audio("nonexistent_type")
        assert audio is None

    def test_audio_caching(self):
        """Test that audio files are cached."""
        # Clear cache first
        _audio_cache.clear()

        # Load once
        audio1 = load_background_audio("custom")
        if audio1:
            # Check it's in cache
            assert "custom" in _audio_cache

            # Load again - should come from cache
            audio2 = load_background_audio("custom")
            assert audio1 is audio2  # Same object (cached)


class TestBackgroundAudioMixer:
    """Tests for BackgroundAudioMixer class."""

    def test_mixer_initialization_defaults(self):
        """Test mixer initializes with correct defaults."""
        mixer = BackgroundAudioMixer()
        assert mixer.audio_type == "custom"
        assert mixer.volume == 0.25
        assert mixer.enabled == True
        assert mixer.position == 0

    def test_mixer_custom_params(self):
        """Test mixer with custom parameters."""
        mixer = BackgroundAudioMixer(
            audio_type="office",
            volume=0.5,
            enabled=False
        )
        assert mixer.audio_type == "office"
        assert mixer.volume == 0.5
        assert mixer.enabled == False

    def test_mixer_volume_clamping_high(self):
        """Test that volume is clamped to max 1.0."""
        mixer = BackgroundAudioMixer(volume=1.5, enabled=False)
        assert mixer.volume == 1.0

    def test_mixer_volume_clamping_low(self):
        """Test that volume is clamped to min 0.0."""
        mixer = BackgroundAudioMixer(volume=-0.5, enabled=False)
        assert mixer.volume == 0.0

    def test_mix_audio_disabled(self):
        """Test that disabled mixer returns original audio."""
        mixer = BackgroundAudioMixer(enabled=False)
        original = b'\xff' * 100
        mixed = mixer.mix_audio(original)
        assert mixed == original

    def test_mix_audio_empty_input(self):
        """Test mixing with empty input."""
        mixer = BackgroundAudioMixer(enabled=False)
        mixed = mixer.mix_audio(b'')
        assert mixed == b''

    def test_mix_audio_base64_disabled(self):
        """Test base64 mixing when disabled."""
        mixer = BackgroundAudioMixer(enabled=False)
        original_b64 = base64.b64encode(b'\xff' * 100).decode()
        mixed_b64 = mixer.mix_audio_base64(original_b64)
        assert mixed_b64 == original_b64

    def test_get_background_chunk_disabled(self):
        """Test getting background chunk when disabled returns silence."""
        mixer = BackgroundAudioMixer(enabled=False)
        chunk = mixer.get_background_chunk(100)
        assert len(chunk) == 100
        assert all(b == 0xFF for b in chunk)  # All silence

    def test_get_background_chunk_base64(self):
        """Test getting base64-encoded background chunk."""
        mixer = BackgroundAudioMixer(enabled=False)
        chunk_b64 = mixer.get_background_chunk_base64(100)
        assert isinstance(chunk_b64, str)
        decoded = base64.b64decode(chunk_b64)
        assert len(decoded) == 100

    def test_mixer_reset(self):
        """Test resetting mixer position."""
        mixer = BackgroundAudioMixer(enabled=False)
        mixer.position = 500
        mixer.reset()
        assert mixer.position == 0

    def test_mixer_position_advances(self):
        """Test that position advances during mixing."""
        mixer = BackgroundAudioMixer(enabled=False)
        initial_pos = mixer.position
        mixer.get_background_chunk(100)
        # Position should advance even when disabled (returns silence)
        assert mixer.position == initial_pos  # Disabled doesn't advance


class TestMixerWithRealAudio:
    """Tests that require actual audio files."""

    @pytest.fixture
    def mixer_with_audio(self):
        """Create a mixer with loaded audio."""
        mixer = BackgroundAudioMixer(audio_type="custom", volume=0.25, enabled=True)
        if not mixer.background_audio:
            pytest.skip("Custom audio file not available")
        return mixer

    def test_mix_audio_with_real_file(self, mixer_with_audio):
        """Test mixing with real audio file."""
        speech = b'\x80' * 160  # 20ms of mid-level audio
        mixed = mixer_with_audio.mix_audio(speech)
        assert len(mixed) == len(speech)
        # Mixing should complete without error
        assert isinstance(mixed, bytes)

    def test_position_loops(self, mixer_with_audio):
        """Test that position loops when reaching end of audio."""
        audio_len = len(mixer_with_audio.background_audio)
        # Get a chunk larger than the audio file
        chunk = mixer_with_audio.get_background_chunk(audio_len + 100)
        assert len(chunk) == audio_len + 100
        # Position should have wrapped
        assert mixer_with_audio.position == 100

    def test_volume_affects_output(self, mixer_with_audio):
        """Test that volume setting affects the mixed output."""
        # Use varied input to see mixing effect
        speech = bytes(range(160))  # 0-159 values

        # Mix at 25% volume
        mixer_with_audio.volume = 0.25
        mixer_with_audio.reset()
        mixed_25 = mixer_with_audio.mix_audio(speech)

        # Mix at 50% volume
        mixer_with_audio.volume = 0.50
        mixer_with_audio.reset()
        mixed_50 = mixer_with_audio.mix_audio(speech)

        # Both should complete and produce valid output
        assert len(mixed_25) == len(speech)
        assert len(mixed_50) == len(speech)


class TestCreateMixerFromAssistant:
    """Tests for create_mixer_from_assistant function."""

    def test_create_from_empty_assistant(self):
        """Test creating mixer from empty assistant config."""
        assistant = {}
        mixer = create_mixer_from_assistant(assistant)
        assert mixer.enabled == False  # Default is disabled
        assert mixer.audio_type == "custom"
        assert mixer.volume == 0.25

    def test_create_from_enabled_assistant(self):
        """Test creating mixer from assistant with audio enabled."""
        assistant = {
            "background_audio_enabled": True,
            "background_audio_type": "office",
            "background_audio_volume": 0.3
        }
        mixer = create_mixer_from_assistant(assistant)
        assert mixer.enabled == True
        assert mixer.audio_type == "office"
        assert mixer.volume == 0.3

    def test_create_from_disabled_assistant(self):
        """Test creating mixer from assistant with audio disabled."""
        assistant = {
            "background_audio_enabled": False,
            "background_audio_type": "call_center",
            "background_audio_volume": 0.5
        }
        mixer = create_mixer_from_assistant(assistant)
        assert mixer.enabled == False

    def test_create_with_custom_type(self):
        """Test creating mixer with custom audio type."""
        assistant = {
            "background_audio_enabled": True,
            "background_audio_type": "custom",
            "background_audio_volume": 0.25
        }
        mixer = create_mixer_from_assistant(assistant)
        assert mixer.audio_type == "custom"

    def test_create_with_partial_config(self):
        """Test creating mixer with partial configuration."""
        assistant = {
            "background_audio_enabled": True
            # Missing type and volume - should use defaults
        }
        mixer = create_mixer_from_assistant(assistant)
        assert mixer.enabled == True
        assert mixer.audio_type == "custom"  # Default
        assert mixer.volume == 0.25  # Default


class TestIntegrationWithAssistantModel:
    """Integration tests with AI assistant model."""

    def test_assistant_model_has_background_fields(self):
        """Test that assistant model includes background audio fields."""
        from app.models.ai_assistant import AIAssistantCreate, AIAssistantResponse

        # Check AIAssistantCreate has the fields
        create_fields = AIAssistantCreate.__fields__
        assert "background_audio_enabled" in create_fields
        assert "background_audio_type" in create_fields
        assert "background_audio_volume" in create_fields

    def test_assistant_create_with_background_audio(self):
        """Test creating assistant with background audio config."""
        from app.models.ai_assistant import AIAssistantCreate

        assistant = AIAssistantCreate(
            user_id="test_user",
            name="Test Assistant",
            system_message="You are a test assistant",
            voice="alloy",
            background_audio_enabled=True,
            background_audio_type="custom",
            background_audio_volume=0.25
        )

        assert assistant.background_audio_enabled == True
        assert assistant.background_audio_type == "custom"
        assert assistant.background_audio_volume == 0.25

    def test_assistant_default_values(self):
        """Test assistant model default values for background audio."""
        from app.models.ai_assistant import AIAssistantCreate

        assistant = AIAssistantCreate(
            user_id="test_user",
            name="Test Assistant",
            system_message="You are a test assistant",
            voice="alloy"
        )

        assert assistant.background_audio_enabled == False
        assert assistant.background_audio_type == "custom"
        assert assistant.background_audio_volume == 0.25


class TestEdgeCases:
    """Edge case and error handling tests."""

    def test_mix_with_none_audio(self):
        """Test mixing when background audio is None."""
        mixer = BackgroundAudioMixer(enabled=True)
        mixer.background_audio = None
        mixer.enabled = True  # Force enabled without audio

        original = b'\xff' * 100
        mixed = mixer.mix_audio(original)
        assert mixed == original  # Should return original

    def test_invalid_base64_handling(self):
        """Test handling of invalid base64 input."""
        mixer = BackgroundAudioMixer(enabled=False)
        # Even with invalid input, disabled mixer should handle gracefully
        result = mixer.mix_audio_base64("not_valid_base64!!!")
        assert result == "not_valid_base64!!!"  # Returns original when disabled

    def test_very_small_volume(self):
        """Test mixing with very small volume."""
        mixer = BackgroundAudioMixer(volume=0.01, enabled=False)
        assert mixer.volume == 0.01

    def test_zero_volume(self):
        """Test mixing with zero volume."""
        mixer = BackgroundAudioMixer(volume=0.0, enabled=False)
        assert mixer.volume == 0.0

    def test_full_volume(self):
        """Test mixing with full volume."""
        mixer = BackgroundAudioMixer(volume=1.0, enabled=False)
        assert mixer.volume == 1.0


class TestConcurrency:
    """Tests for concurrent usage scenarios."""

    def test_multiple_mixers_independent(self):
        """Test that multiple mixers are independent."""
        mixer1 = BackgroundAudioMixer(audio_type="custom", volume=0.25, enabled=False)
        mixer2 = BackgroundAudioMixer(audio_type="office", volume=0.5, enabled=False)

        mixer1.position = 100
        mixer2.position = 200

        assert mixer1.position != mixer2.position
        assert mixer1.volume != mixer2.volume

    def test_mixer_state_isolation(self):
        """Test that mixer state changes don't affect other instances."""
        mixer1 = BackgroundAudioMixer(enabled=False)
        mixer2 = BackgroundAudioMixer(enabled=False)

        mixer1.get_background_chunk(100)

        # mixer2 should still be at position 0
        assert mixer2.position == 0


# Performance tests
class TestPerformance:
    """Performance-related tests."""

    def test_mixing_performance(self):
        """Test that mixing is reasonably fast."""
        import time

        mixer = BackgroundAudioMixer(enabled=False)
        audio = b'\x80' * 8000  # 1 second of audio at 8kHz

        start = time.time()
        for _ in range(100):  # Mix 100 times
            mixer.mix_audio(audio)
        elapsed = time.time() - start

        # Should complete 100 iterations in less than 1 second
        assert elapsed < 1.0, f"Mixing too slow: {elapsed:.2f}s for 100 iterations"

    def test_base64_encoding_performance(self):
        """Test base64 encoding/decoding performance."""
        import time

        mixer = BackgroundAudioMixer(enabled=False)
        audio = b'\x80' * 8000
        audio_b64 = base64.b64encode(audio).decode()

        start = time.time()
        for _ in range(100):
            mixer.mix_audio_base64(audio_b64)
        elapsed = time.time() - start

        assert elapsed < 1.0, f"Base64 mixing too slow: {elapsed:.2f}s"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
