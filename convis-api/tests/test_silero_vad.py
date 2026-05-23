"""
Unit tests for Silero VAD (Voice Activity Detection)
Tests mulaw decoding, resampling, and speech detection logic.
"""
import pytest
import numpy as np
from unittest.mock import Mock, MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class TestMulawDecoding:
    """Test mulaw to linear PCM conversion"""

    @pytest.fixture
    def vad_processor(self):
        """Create VAD processor with mocked model"""
        with patch('app.utils.silero_vad.load_silero_vad') as mock_load:
            mock_model = MagicMock()
            mock_utils = MagicMock()
            mock_load.return_value = (mock_model, mock_utils)

            from app.utils.silero_vad import SileroVADProcessor
            processor = SileroVADProcessor(threshold=0.5)
            return processor

    def test_mulaw_silence_decoding(self, vad_processor):
        """Test decoding silence (0xFF in mulaw)"""
        # 0xFF in mulaw represents silence (near-zero amplitude)
        silence_data = bytes([0xFF] * 10)
        result = vad_processor.mulaw_to_linear(silence_data)

        assert len(result) == 10
        assert result.dtype == np.float32
        # Silence should be near zero
        assert all(abs(s) < 0.01 for s in result)

    def test_mulaw_positive_peak_decoding(self, vad_processor):
        """Test decoding positive peak in mulaw"""
        # In G.711 mulaw, after bit inversion:
        # 0x80 (inverted to 0x7F) = positive max (sign bit 0 = positive)
        peak_data = bytes([0x80])
        result = vad_processor.mulaw_to_linear(peak_data)

        assert len(result) == 1
        # Should be large positive value (close to 1.0)
        assert result[0] > 0.5

    def test_mulaw_negative_peak_decoding(self, vad_processor):
        """Test decoding negative peak in mulaw"""
        # In G.711 mulaw, after bit inversion:
        # 0x00 (inverted to 0xFF) = negative max (sign bit 1 = negative)
        peak_data = bytes([0x00])
        result = vad_processor.mulaw_to_linear(peak_data)

        assert len(result) == 1
        # Should be large negative value (close to -1.0)
        assert result[0] < -0.5

    def test_mulaw_output_range(self, vad_processor):
        """Test that all mulaw values decode to [-1, 1] range"""
        # Test all possible mulaw values
        all_values = bytes(range(256))
        result = vad_processor.mulaw_to_linear(all_values)

        assert len(result) == 256
        assert all(-1.0 <= s <= 1.0 for s in result)

    def test_mulaw_symmetry(self, vad_processor):
        """Test that positive and negative mulaw values are symmetric"""
        # Complementary mulaw values should produce symmetric linear values
        for i in range(128):
            positive = bytes([i])
            negative = bytes([i | 0x80])  # Set sign bit

            pos_result = vad_processor.mulaw_to_linear(positive)[0]
            neg_result = vad_processor.mulaw_to_linear(negative)[0]

            # Should be approximately equal magnitude, opposite sign
            assert abs(abs(pos_result) - abs(neg_result)) < 0.1


class TestAudioResampling:
    """Test 8kHz to 16kHz resampling"""

    @pytest.fixture
    def vad_processor(self):
        """Create VAD processor with mocked model"""
        with patch('app.utils.silero_vad.load_silero_vad') as mock_load:
            mock_model = MagicMock()
            mock_utils = MagicMock()
            mock_load.return_value = (mock_model, mock_utils)

            from app.utils.silero_vad import SileroVADProcessor
            processor = SileroVADProcessor(threshold=0.5)
            return processor

    def test_resample_doubles_length(self, vad_processor):
        """Test that resampling doubles the sample count"""
        audio_8k = np.array([0.1, 0.2, 0.3, 0.4, 0.5], dtype=np.float32)
        audio_16k = vad_processor.resample_8k_to_16k(audio_8k)

        assert len(audio_16k) == len(audio_8k) * 2

    def test_resample_preserves_dtype(self, vad_processor):
        """Test that resampling preserves float32 dtype"""
        audio_8k = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        audio_16k = vad_processor.resample_8k_to_16k(audio_8k)

        assert audio_16k.dtype == np.float32

    def test_resample_preserves_endpoints(self, vad_processor):
        """Test that first and last samples are preserved"""
        audio_8k = np.array([0.1, 0.5, 0.3, 0.8], dtype=np.float32)
        audio_16k = vad_processor.resample_8k_to_16k(audio_8k)

        # First sample should be preserved
        assert abs(audio_16k[0] - audio_8k[0]) < 0.01
        # Last sample should be preserved
        assert abs(audio_16k[-1] - audio_8k[-1]) < 0.01

    def test_resample_interpolates_correctly(self, vad_processor):
        """Test linear interpolation between samples"""
        # Simple case: [0, 1] should become [0, 0.33, 0.67, 1] approximately
        audio_8k = np.array([0.0, 1.0], dtype=np.float32)
        audio_16k = vad_processor.resample_8k_to_16k(audio_8k)

        # Should be monotonically increasing
        for i in range(len(audio_16k) - 1):
            assert audio_16k[i] <= audio_16k[i + 1]


class TestVADStateManagement:
    """Test VAD state machine logic"""

    @pytest.fixture
    def vad_processor(self):
        """Create VAD processor with mocked model"""
        with patch('app.utils.silero_vad.load_silero_vad') as mock_load:
            mock_model = MagicMock()
            mock_utils = MagicMock()
            mock_load.return_value = (mock_model, mock_utils)

            from app.utils.silero_vad import SileroVADProcessor
            processor = SileroVADProcessor(
                threshold=0.5,
                min_speech_duration_ms=250,
                min_silence_duration_ms=300
            )
            return processor

    def test_initial_state(self, vad_processor):
        """Test initial VAD state"""
        assert vad_processor.is_speaking == False
        assert vad_processor.speech_start_ms == 0
        assert vad_processor.silence_start_ms == 0
        assert vad_processor.current_time_ms == 0

    def test_reset_clears_state(self, vad_processor):
        """Test that reset clears all state"""
        # Modify state
        vad_processor.is_speaking = True
        vad_processor.speech_start_ms = 1000
        vad_processor.silence_start_ms = 2000
        vad_processor.current_time_ms = 3000
        vad_processor.audio_buffer = [1, 2, 3]

        vad_processor.reset()

        assert vad_processor.is_speaking == False
        assert vad_processor.speech_start_ms == 0
        assert vad_processor.silence_start_ms == 0
        assert vad_processor.current_time_ms == 0
        assert vad_processor.audio_buffer == []

    def test_speech_detection_starts_speaking(self, vad_processor):
        """Test that high probability starts speaking state"""
        # Simulate speech detected
        is_speech, prob = vad_processor._update_state(0.8)  # Above threshold

        assert is_speech == True
        assert vad_processor.is_speaking == True
        assert vad_processor.speech_start_ms == 0  # Starts at current time

    def test_silence_detection_during_speech(self, vad_processor):
        """Test silence detection while speaking"""
        # First detect speech
        vad_processor._update_state(0.8)
        vad_processor.current_time_ms = 100

        # Now detect silence
        is_speech, prob = vad_processor._update_state(0.2)  # Below threshold

        assert is_speech == False
        assert vad_processor.is_speaking == True  # Still speaking (not enough silence)
        assert vad_processor.silence_start_ms == 100  # Silence started

    def test_speech_ended_after_silence_duration(self, vad_processor):
        """Test that speech ends after sufficient silence"""
        # Start speech
        vad_processor._update_state(0.8)
        vad_processor.current_time_ms = 300  # 300ms of speech

        # Start silence
        vad_processor._update_state(0.2)
        vad_processor.current_time_ms = 700  # 400ms of silence (> 300ms threshold)

        # Continue silence
        vad_processor._update_state(0.2)

        assert vad_processor.is_speaking == False  # Speech should have ended

    def test_is_speech_ended_returns_false_while_speaking(self, vad_processor):
        """Test is_speech_ended returns False while speaking"""
        vad_processor.is_speaking = True

        assert vad_processor.is_speech_ended() == False

    def test_get_speech_duration_ms(self, vad_processor):
        """Test speech duration calculation"""
        vad_processor.speech_start_ms = 100
        vad_processor.silence_start_ms = 600

        duration = vad_processor.get_speech_duration_ms()

        assert duration == 500  # 600 - 100

    def test_get_speech_duration_returns_zero_when_no_speech(self, vad_processor):
        """Test speech duration returns 0 when no speech recorded"""
        duration = vad_processor.get_speech_duration_ms()

        assert duration == 0


class TestVADProcessChunk:
    """Test the full process_chunk pipeline"""

    @pytest.fixture
    def vad_processor(self):
        """Create VAD processor with mocked model"""
        with patch('app.utils.silero_vad.load_silero_vad') as mock_load:
            mock_model = MagicMock()
            mock_utils = MagicMock()
            mock_load.return_value = (mock_model, mock_utils)

            from app.utils.silero_vad import SileroVADProcessor
            processor = SileroVADProcessor(threshold=0.5)
            # Mock the model call to return speech probability
            processor.model = MagicMock(return_value=MagicMock(item=MagicMock(return_value=0.7)))
            # Mark model as loaded (since we're using lazy loading now)
            processor._model_loaded = True
            return processor

    def test_process_chunk_accumulates_buffer(self, vad_processor):
        """Test that audio is accumulated in buffer"""
        # Generate some mulaw audio (100 bytes = 100 samples at 8kHz = 12.5ms)
        mulaw_audio = bytes([0x80] * 100)

        with patch('torch.from_numpy'):
            vad_processor.process_chunk(mulaw_audio)

        # Buffer should have some samples (200 after resampling to 16kHz)
        # But less than 512 (chunk size), so some remain
        assert len(vad_processor.audio_buffer) > 0

    def test_process_chunk_returns_speech_state(self, vad_processor):
        """Test that process_chunk returns speech detection result"""
        mulaw_audio = bytes([0x80] * 512)  # Enough for one 512-sample chunk at 16kHz

        with patch('torch.from_numpy'):
            is_speech, prob = vad_processor.process_chunk(mulaw_audio)

        assert isinstance(is_speech, bool)
        assert isinstance(prob, float)


class TestConvenienceFunctions:
    """Test one-shot convenience functions"""

    def test_detect_speech_in_audio(self):
        """Test one-shot speech detection"""
        with patch('app.utils.silero_vad.SileroVADProcessor') as MockProcessor:
            mock_instance = MagicMock()
            mock_instance.process_chunk.return_value = (True, 0.8)
            MockProcessor.return_value = mock_instance

            from app.utils.silero_vad import detect_speech_in_audio

            result = detect_speech_in_audio(b'\x80' * 100)

            assert result == True
            mock_instance.process_chunk.assert_called_once()


class TestVADConfiguration:
    """Test VAD configuration options"""

    def test_custom_threshold(self):
        """Test custom threshold configuration"""
        with patch('app.utils.silero_vad.load_silero_vad') as mock_load:
            mock_model = MagicMock()
            mock_utils = MagicMock()
            mock_load.return_value = (mock_model, mock_utils)

            from app.utils.silero_vad import SileroVADProcessor
            processor = SileroVADProcessor(threshold=0.8)

            assert processor.threshold == 0.8

    def test_custom_timing_parameters(self):
        """Test custom timing parameters"""
        with patch('app.utils.silero_vad.load_silero_vad') as mock_load:
            mock_model = MagicMock()
            mock_utils = MagicMock()
            mock_load.return_value = (mock_model, mock_utils)

            from app.utils.silero_vad import SileroVADProcessor
            processor = SileroVADProcessor(
                min_speech_duration_ms=500,
                min_silence_duration_ms=600,
                speech_pad_ms=200
            )

            assert processor.min_speech_duration_ms == 500
            assert processor.min_silence_duration_ms == 600
            assert processor.speech_pad_ms == 200


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
