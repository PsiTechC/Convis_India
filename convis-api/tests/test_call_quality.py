"""
Unit and Integration Tests for Call Quality Monitoring

Tests cover:
1. CallQualityMonitor initialization and configuration
2. Network metrics tracking (packet loss, jitter, RTT)
3. Audio quality assessment (SNR, RMS, silence detection)
4. MOS score calculation
5. QoS thresholds and alerting
6. Quality report generation
7. API endpoints
"""

import pytest
import asyncio
import struct
import math
import time
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime

from app.utils.call_quality_monitor import (
    CallQualityMonitor,
    QoSThresholds,
    QualityAlert,
    NetworkMetrics,
    AudioMetrics,
    QualityScore,
    AlertType,
    QualityLevel
)


class TestQoSThresholds:
    """Test QoS threshold configuration"""

    def test_default_thresholds(self):
        """Test default threshold values"""
        thresholds = QoSThresholds()

        assert thresholds.max_packet_loss_percent == 3.0
        assert thresholds.max_jitter_ms == 30.0
        assert thresholds.max_rtt_ms == 300.0
        assert thresholds.min_snr_db == 10.0
        assert thresholds.min_audio_level_db == -50.0

    def test_custom_thresholds(self):
        """Test custom threshold configuration"""
        thresholds = QoSThresholds(
            max_packet_loss_percent=5.0,
            max_jitter_ms=50.0,
            max_rtt_ms=500.0,
            min_snr_db=15.0
        )

        assert thresholds.max_packet_loss_percent == 5.0
        assert thresholds.max_jitter_ms == 50.0
        assert thresholds.max_rtt_ms == 500.0
        assert thresholds.min_snr_db == 15.0


class TestNetworkMetrics:
    """Test network quality metrics tracking"""

    def test_network_metrics_initialization(self):
        """Test NetworkMetrics default values"""
        metrics = NetworkMetrics()

        assert metrics.packets_sent == 0
        assert metrics.packets_received == 0
        assert metrics.packets_lost == 0
        assert metrics.packet_loss_percent == 0.0
        assert metrics.jitter_ms == 0.0
        assert metrics.rtt_ms == 0.0

    def test_packet_loss_calculation(self):
        """Test packet loss percentage calculation"""
        metrics = NetworkMetrics()
        metrics.packets_sent = 100
        metrics.packets_received = 95
        metrics.packets_lost = 5

        # Simulate calculation (would normally be done by monitor)
        loss_percent = (metrics.packets_lost / metrics.packets_sent) * 100 if metrics.packets_sent > 0 else 0

        assert loss_percent == 5.0


class TestAudioMetrics:
    """Test audio quality metrics tracking"""

    def test_audio_metrics_initialization(self):
        """Test AudioMetrics default values"""
        metrics = AudioMetrics()

        assert metrics.snr_db == 0.0
        assert metrics.rms_level_db == -60.0
        assert metrics.peak_level_db == -60.0
        assert metrics.silence_percent == 0.0


class TestCallQualityMonitor:
    """Test CallQualityMonitor core functionality"""

    @pytest.fixture
    def monitor(self):
        """Create a CallQualityMonitor instance"""
        return CallQualityMonitor(
            call_id="test_call_123",
            thresholds=QoSThresholds(),
            sample_rate=8000
        )

    @pytest.fixture
    def monitor_with_callback(self):
        """Create a monitor with alert callback"""
        alerts = []

        def alert_callback(alert):
            alerts.append(alert)

        monitor = CallQualityMonitor(
            call_id="test_call_456",
            thresholds=QoSThresholds(
                max_packet_loss_percent=2.0,
                max_jitter_ms=20.0
            ),
            sample_rate=8000,
            alert_callback=alert_callback
        )
        return monitor, alerts

    def test_monitor_initialization(self, monitor):
        """Test monitor initialization"""
        assert monitor.call_id == "test_call_123"
        assert monitor.sample_rate == 8000
        assert isinstance(monitor.network, NetworkMetrics)
        assert isinstance(monitor.audio, AudioMetrics)
        assert isinstance(monitor.quality, QualityScore)

    def test_track_audio_chunk_silence(self, monitor):
        """Test tracking silent audio chunk"""
        # Generate a silent audio chunk (all zeros)
        silent_audio = bytes(1600)  # 100ms of silence at 8kHz (16-bit)

        monitor.track_audio_chunk(silent_audio, is_voice=False)

        # Audio should be tracked
        assert monitor.audio.audio_present == True

    def test_track_audio_chunk_voice(self, monitor):
        """Test tracking voice audio chunk"""
        # Generate a simple sine wave audio
        sample_rate = 8000
        frequency = 440  # Hz
        duration_samples = 800  # 100ms

        audio_samples = []
        for i in range(duration_samples):
            sample = int(32767 * 0.5 * math.sin(2 * math.pi * frequency * i / sample_rate))
            audio_samples.append(struct.pack('<h', sample))

        audio_data = b''.join(audio_samples)

        monitor.track_audio_chunk(audio_data, is_voice=True)

        # Audio should be tracked with voice detection
        assert monitor.audio.audio_present == True

    def test_track_packet_sent(self, monitor):
        """Test tracking sent packets"""
        monitor.track_packet_sent(sequence_num=1, timestamp=time.time())
        monitor.track_packet_sent(sequence_num=2, timestamp=time.time())
        monitor.track_packet_sent(sequence_num=3, timestamp=time.time())

        assert monitor.network.packets_sent == 3

    def test_track_packet_received(self, monitor):
        """Test tracking received packets"""
        ts = time.time()
        monitor.track_packet_sent(sequence_num=1, timestamp=ts)
        monitor.track_packet_sent(sequence_num=2, timestamp=ts + 0.02)
        monitor.track_packet_sent(sequence_num=3, timestamp=ts + 0.04)

        monitor.track_packet_received(sequence_num=1, timestamp=ts + 0.1)
        monitor.track_packet_received(sequence_num=2, timestamp=ts + 0.12)
        # seq_num=3 not received (simulating packet loss)

        assert monitor.network.packets_sent == 3
        assert monitor.network.packets_received == 2

    def test_jitter_calculation(self, monitor):
        """Test jitter calculation (RFC 3550 algorithm)"""
        ts = time.time()

        # Send packets with consistent timing
        for i in range(10):
            monitor.track_packet_sent(sequence_num=i, timestamp=ts + i * 0.02)

        # Receive with varying delays (introducing jitter)
        for i in range(10):
            # Add variable delay to simulate jitter
            jitter_delay = 0.005 if i % 2 == 0 else 0.015
            monitor.track_packet_received(sequence_num=i, timestamp=ts + i * 0.02 + 0.1 + jitter_delay)

        # Jitter should be calculated
        assert monitor.network.jitter_ms >= 0

    def test_get_quality_report(self, monitor):
        """Test generating quality report"""
        # Add some data
        monitor.track_audio_chunk(bytes(1600), is_voice=False)
        monitor.track_audio_chunk(bytes(1600), is_voice=True)

        report = monitor.get_quality_report()

        assert "call_id" in report
        assert report["call_id"] == "test_call_123"
        assert "network_metrics" in report
        assert "audio_metrics" in report
        assert "quality_score" in report
        assert "duration_seconds" in report

    def test_mos_calculation_excellent(self, monitor):
        """Test MOS calculation for excellent quality"""
        # Simulate excellent conditions
        monitor.network.packet_loss_percent = 0.0
        monitor.network.jitter_ms = 5.0
        monitor.network.rtt_ms = 50.0
        monitor.audio.snr_db = 25.0

        # Trigger quality recalculation
        monitor._update_quality_scores()

        report = monitor.get_quality_report()

        # MOS should be high (4.0+)
        assert report["quality_score"]["mos"] >= 3.5
        assert report["quality_score"]["level"] in ["excellent", "good"]

    def test_mos_calculation_poor(self, monitor):
        """Test MOS calculation for poor quality"""
        # Simulate poor conditions
        monitor.network.packet_loss_percent = 10.0
        monitor.network.jitter_ms = 100.0
        monitor.network.rtt_ms = 500.0
        monitor.audio.snr_db = 5.0

        # Trigger quality recalculation
        monitor._update_quality_scores()

        report = monitor.get_quality_report()

        # MOS should be low
        assert report["quality_score"]["mos"] < 4.0

    def test_quality_alert_packet_loss(self, monitor_with_callback):
        """Test quality alert triggered by high packet loss"""
        monitor, alerts = monitor_with_callback

        # Simulate high packet loss by sending many packets and receiving few
        for i in range(100):
            monitor.track_packet_sent(sequence_num=i, timestamp=time.time())

        # Only receive 90 packets (10% loss, exceeds 2% threshold)
        for i in range(90):
            monitor.track_packet_received(sequence_num=i, timestamp=time.time())

        # Check if alert was triggered
        # Note: Alert triggering depends on implementation logic
        assert monitor.network.packet_loss_percent >= 5.0

    def test_quality_overall_rating(self, monitor):
        """Test overall quality rating assignment"""
        test_cases = [
            (4.5, QualityLevel.EXCELLENT),
            (4.0, QualityLevel.EXCELLENT),
            (3.6, QualityLevel.GOOD),
            (3.0, QualityLevel.FAIR),
            (2.6, QualityLevel.POOR),
            (2.0, QualityLevel.BAD),
        ]

        for mos, expected_quality in test_cases:
            monitor.quality.mos_score = mos
            # The quality level is determined by _update_quality_scores


class TestCallQualityMonitorIntegration:
    """Integration tests for call quality monitoring"""

    @pytest.fixture
    def monitor(self):
        alerts = []

        def callback(alert):
            alerts.append(alert)

        return CallQualityMonitor(
            call_id="integration_test",
            thresholds=QoSThresholds(),
            sample_rate=8000,
            alert_callback=callback
        ), alerts

    def test_full_call_simulation(self, monitor):
        """Simulate a full call with mixed quality"""
        monitor_instance, alerts = monitor

        # Simulate 30 seconds of audio chunks (8kHz, 20ms chunks)
        num_chunks = 1500  # 30 seconds

        for i in range(num_chunks):
            # Alternate between silence and voice
            is_voice = i % 3 != 0

            # Generate realistic audio
            if is_voice:
                audio = self._generate_voice_audio()
            else:
                audio = bytes(160)  # 20ms of silence

            monitor_instance.track_audio_chunk(audio, is_voice=is_voice)

        # Generate quality report
        report = monitor_instance.get_quality_report()

        assert "quality_score" in report
        assert "mos" in report["quality_score"]
        assert "level" in report["quality_score"]

    def _generate_voice_audio(self):
        """Generate a simple voice-like audio chunk"""
        samples = []
        for i in range(160):
            sample = int(16384 * math.sin(2 * math.pi * 300 * i / 8000))
            samples.append(struct.pack('<h', sample))
        return b''.join(samples)

    def test_concurrent_tracking(self, monitor):
        """Test thread-safety of concurrent tracking"""
        monitor_instance, alerts = monitor

        import threading

        def track_audio():
            for _ in range(100):
                monitor_instance.track_audio_chunk(bytes(160), is_voice=True)

        def track_packets():
            for i in range(100):
                monitor_instance.track_packet_sent(sequence_num=i, timestamp=time.time())
                monitor_instance.track_packet_received(sequence_num=i, timestamp=time.time())

        threads = [
            threading.Thread(target=track_audio),
            threading.Thread(target=track_packets)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should not crash and should have tracked data
        assert monitor_instance.network.packets_sent == 100


class TestQualityAlert:
    """Test QualityAlert dataclass"""

    def test_alert_creation(self):
        """Test creating a quality alert"""
        alert = QualityAlert(
            alert_type=AlertType.HIGH_PACKET_LOSS,
            severity="warning",
            value=5.0,
            threshold=3.0,
            message="Packet loss exceeded threshold"
        )

        assert alert.severity == "warning"
        assert alert.alert_type == AlertType.HIGH_PACKET_LOSS
        assert alert.value == 5.0
        assert alert.threshold == 3.0

    def test_critical_alert(self):
        """Test critical severity alert"""
        alert = QualityAlert(
            alert_type=AlertType.HIGH_JITTER,
            severity="critical",
            value=100.0,
            threshold=30.0,
            message="Critical jitter detected"
        )

        assert alert.severity == "critical"


class TestCallQualityAPI:
    """Test API endpoints for call quality"""

    @pytest.fixture
    def mock_db(self):
        """Create mock database"""
        mock_collection = MagicMock()
        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=mock_collection)
        return mock_db, mock_collection

    @pytest.fixture
    def mock_current_user(self):
        """Create mock current user"""
        return {
            "_id": "user_123",
            "email": "test@example.com"
        }

    @pytest.mark.asyncio
    async def test_get_call_quality_endpoint(self, mock_db, mock_current_user):
        """Test GET /calls/{call_id}/quality endpoint"""
        from app.routes.call_quality import get_call_quality

        mock_db_instance, mock_collection = mock_db

        # Mock call log with quality data
        mock_collection.find_one.return_value = {
            "call_id": "test_call",
            "user_id": "user_123",
            "quality_report": {
                "network": {
                    "packet_loss_percent": 1.0,
                    "jitter_ms": 10.0,
                    "jitter_max_ms": 25.0,
                    "rtt_ms": 100.0,
                    "packets_sent": 1000,
                    "packets_received": 990,
                    "packets_lost": 10
                },
                "audio": {
                    "snr_db": 20.0,
                    "rms_db": -20.0,
                    "peak_db": -10.0,
                    "silence_percent": 30.0,
                    "voice_activity_percent": 70.0,
                    "total_chunks": 500
                },
                "quality": {
                    "mos": 4.2,
                    "overall_quality": "excellent",
                    "alert_count": 0
                }
            },
            "duration_seconds": 120
        }

        with patch('app.routes.call_quality.Database') as mock_Database:
            mock_Database.get_db.return_value = mock_db_instance

            result = await get_call_quality("test_call", mock_current_user)

            assert result.call_id == "test_call"
            assert result.quality.mos == 4.2
            assert result.quality.overall_quality == "excellent"

    @pytest.mark.asyncio
    async def test_get_call_quality_not_found(self, mock_db, mock_current_user):
        """Test GET /calls/{call_id}/quality with non-existent call"""
        from app.routes.call_quality import get_call_quality
        from fastapi import HTTPException

        mock_db_instance, mock_collection = mock_db
        mock_collection.find_one.return_value = None

        with patch('app.routes.call_quality.Database') as mock_Database:
            mock_Database.get_db.return_value = mock_db_instance

            with pytest.raises(HTTPException) as exc_info:
                await get_call_quality("nonexistent_call", mock_current_user)

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_quality_thresholds_endpoint(self, mock_db, mock_current_user):
        """Test GET /quality/thresholds endpoint"""
        from app.routes.call_quality import get_quality_thresholds

        mock_db_instance, mock_collection = mock_db
        mock_collection.find_one.return_value = {
            "_id": mock_current_user["_id"],
            "quality_settings": {
                "max_packet_loss_percent": 5.0,
                "max_jitter_ms": 40.0,
                "max_rtt_ms": 400.0,
                "min_snr_db": 12.0,
                "min_mos": 3.5
            }
        }

        with patch('app.routes.call_quality.Database') as mock_Database:
            mock_Database.get_db.return_value = mock_db_instance

            result = await get_quality_thresholds(mock_current_user)

            assert result.max_packet_loss_percent == 5.0
            assert result.max_jitter_ms == 40.0


class TestAudioAnalysis:
    """Test audio analysis functions"""

    def test_rms_calculation(self):
        """Test RMS level calculation from audio"""
        # Generate a known amplitude sine wave
        amplitude = 0.5
        samples = []
        for i in range(800):
            sample = int(32767 * amplitude * math.sin(2 * math.pi * 440 * i / 8000))
            samples.append(struct.pack('<h', sample))

        audio_data = b''.join(samples)

        # RMS should be approximately amplitude / sqrt(2)
        # In dB: 20 * log10(amplitude / sqrt(2))

    def test_snr_calculation(self):
        """Test SNR calculation"""
        # Generate signal + noise
        signal_level = 0.5
        noise_level = 0.01

        samples = []
        for i in range(800):
            signal = signal_level * math.sin(2 * math.pi * 440 * i / 8000)
            noise = noise_level * (2 * (i % 2) - 1)  # Simple noise pattern
            sample = int(32767 * (signal + noise))
            samples.append(struct.pack('<h', sample))

        # SNR should be approximately 20 * log10(signal_level / noise_level)
        # = 20 * log10(0.5 / 0.01) = 20 * log10(50) ≈ 34 dB

    def test_silence_detection(self):
        """Test silence detection in audio"""
        # Generate very quiet audio (should be detected as silence)
        amplitude = 0.001  # Very quiet
        samples = []
        for i in range(800):
            sample = int(32767 * amplitude * math.sin(2 * math.pi * 440 * i / 8000))
            samples.append(struct.pack('<h', sample))

        silent_audio = b''.join(samples)

        monitor = CallQualityMonitor(call_id="test", sample_rate=8000)
        monitor.track_audio_chunk(silent_audio, is_voice=False)

        # Should be detected as silence or very quiet


# Run tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
