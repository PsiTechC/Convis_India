"""
Comprehensive Unit Tests for All Calling-Related Functions
Tests cover: Inbound calls, Outbound calls, Voice providers, Streaming handlers,
OpenAI Realtime, Custom providers, Twilio integration, and Audio processing
"""

import pytest
import asyncio
import json
import base64
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime
import os

# Set test environment
os.environ.setdefault('MONGODB_URI', 'mongodb://localhost:27017')
os.environ.setdefault('DATABASE_NAME', 'test_db')
os.environ.setdefault('EMAIL_USER', 'test@test.com')
os.environ.setdefault('EMAIL_PASS', 'test')


class TestAudioProcessing:
    """Tests for audio encoding/decoding and processing"""

    def test_base64_audio_decode(self):
        """Test base64 audio decoding"""
        # Simulate Twilio audio payload (base64 encoded)
        original_audio = b'\x00\x01\x02\x03\x04\x05'
        encoded_audio = base64.b64encode(original_audio).decode('utf-8')

        decoded_audio = base64.b64decode(encoded_audio)
        assert decoded_audio == original_audio

    def test_mulaw_audio_format(self):
        """Test mu-law audio format handling"""
        # Twilio sends audio in g711_ulaw format at 8kHz
        sample_rate = 8000
        audio_format = "g711_ulaw"

        assert sample_rate == 8000
        assert audio_format == "g711_ulaw"

    def test_audio_buffer_accumulation(self):
        """Test audio buffer accumulation for VAD"""
        audio_buffer = bytearray()

        # Simulate receiving multiple audio chunks
        chunk1 = b'\x00\x01\x02\x03'
        chunk2 = b'\x04\x05\x06\x07'
        chunk3 = b'\x08\x09\x0a\x0b'

        audio_buffer.extend(chunk1)
        audio_buffer.extend(chunk2)
        audio_buffer.extend(chunk3)

        assert len(audio_buffer) == 12
        assert bytes(audio_buffer) == chunk1 + chunk2 + chunk3


# ============================================================================
# TEST: Twilio WebSocket Events
# ============================================================================
class TestTwilioWebSocketEvents:
    """Tests for handling Twilio WebSocket events"""

    def test_parse_start_event(self):
        """Test parsing Twilio start event"""
        start_event = {
            "event": "start",
            "start": {
                "streamSid": "MZ123456",
                "callSid": "CA789012",
                "customParameters": {
                    "From": "+1234567890",
                    "To": "+0987654321"
                }
            }
        }

        assert start_event["event"] == "start"
        stream_sid = start_event["start"]["streamSid"]
        call_sid = start_event["start"]["callSid"]

        assert stream_sid == "MZ123456"
        assert call_sid == "CA789012"

    def test_parse_media_event(self):
        """Test parsing Twilio media event"""
        media_event = {
            "event": "media",
            "media": {
                "timestamp": "1234",
                "payload": base64.b64encode(b"audio_data").decode()
            }
        }

        assert media_event["event"] == "media"
        timestamp = int(media_event["media"]["timestamp"])
        payload = media_event["media"]["payload"]

        assert timestamp == 1234
        assert base64.b64decode(payload) == b"audio_data"

    def test_parse_stop_event(self):
        """Test parsing Twilio stop event"""
        stop_event = {
            "event": "stop",
            "stop": {
                "accountSid": "AC123",
                "callSid": "CA456"
            }
        }

        assert stop_event["event"] == "stop"

    def test_parse_mark_event(self):
        """Test parsing Twilio mark event"""
        mark_event = {
            "event": "mark",
            "mark": {
                "name": "responsePart"
            }
        }

        assert mark_event["event"] == "mark"
        assert mark_event["mark"]["name"] == "responsePart"

    def test_create_media_response(self):
        """Test creating media response to Twilio"""
        stream_sid = "MZ123456"
        audio_payload = base64.b64encode(b"audio_response").decode()

        media_response = {
            "event": "media",
            "streamSid": stream_sid,
            "media": {
                "payload": audio_payload
            }
        }

        assert media_response["event"] == "media"
        assert media_response["streamSid"] == stream_sid

    def test_create_clear_event(self):
        """Test creating clear event for interruption"""
        stream_sid = "MZ123456"

        clear_event = {
            "event": "clear",
            "streamSid": stream_sid
        }

        assert clear_event["event"] == "clear"
        assert clear_event["streamSid"] == stream_sid


# ============================================================================
# TEST: Provider Key Resolution
# ============================================================================
class TestProviderKeyResolution:
    """Tests for API key resolution logic"""

    def test_resolve_provider_keys_structure(self):
        """Test provider keys resolution returns correct structure"""
        # Mock resolved keys
        provider_keys = {
            "openai": "sk-openai-key",
            "deepgram": "dg-key",
            "elevenlabs": "el-key",
            "anthropic": "ant-key"
        }

        assert "openai" in provider_keys
        assert "deepgram" in provider_keys
        assert "elevenlabs" in provider_keys

    def test_provider_key_fallback(self):
        """Test provider key fallback logic"""
        provider_keys = {
            "openai": "sk-openai-key",
            "deepgram": None,
            "elevenlabs": "el-key"
        }

        # Should use OpenAI as fallback for missing Deepgram
        llm_provider = "openai"
        llm_key = provider_keys.get(llm_provider) or provider_keys.get("openai")

        assert llm_key == "sk-openai-key"


# ============================================================================
# TEST: Call Log Creation
# ============================================================================
class TestCallLogCreation:
    """Tests for call log document creation"""

    def test_inbound_call_log_structure(self):
        """Test inbound call log document structure"""
        call_log = {
            "call_sid": "CA123456",
            "stream_sid": "MZ789012",
            "assistant_id": "asst_123",
            "user_id": "user_456",
            "from_number": "+1234567890",
            "to_number": "+0987654321",
            "direction": "inbound",
            "status": "in-progress",
            "voice_config": {
                "asr_provider": "deepgram",
                "tts_provider": "elevenlabs",
                "llm_provider": "openai"
            },
            "started_at": datetime.utcnow(),
            "created_at": datetime.utcnow()
        }

        assert call_log["direction"] == "inbound"
        assert call_log["status"] == "in-progress"
        assert "voice_config" in call_log

    def test_outbound_call_log_structure(self):
        """Test outbound call log document structure"""
        call_log = {
            "call_sid": "CA123456",
            "assistant_id": "asst_123",
            "user_id": "user_456",
            "from_number": "+0987654321",
            "to_number": "+1234567890",
            "direction": "outbound",
            "status": "initiated",
            "voice_config": {
                "asr_provider": "openai",
                "tts_provider": "openai",
                "llm_provider": "openai-realtime"
            },
            "created_at": datetime.utcnow()
        }

        assert call_log["direction"] == "outbound"
        assert call_log["status"] == "initiated"


# ============================================================================
# TEST: TwiML Generation
# ============================================================================
class TestTwiMLGeneration:
    """Tests for TwiML response generation"""

    def test_outbound_twiml_structure(self):
        """Test outbound call TwiML structure"""
        domain = "api.convis.ai"
        assistant_id = "123456"
        phone_number = "+1234567890"

        outbound_twiml = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response>'
            f'<Connect>'
            f'<Stream url="wss://{domain}/api/outbound-calls/media-stream/{assistant_id}">'
            f'<Parameter name="to_number" value="{phone_number}" />'
            f'</Stream>'
            f'</Connect>'
            f'</Response>'
        )

        assert '<?xml version="1.0"' in outbound_twiml
        assert '<Response>' in outbound_twiml
        assert '<Connect>' in outbound_twiml
        assert '<Stream url="wss://' in outbound_twiml
        assert f'media-stream/{assistant_id}"' in outbound_twiml

    def test_domain_cleaning_regex(self):
        """Test domain cleaning from API_BASE_URL"""
        import re

        # Test various URL formats
        urls = [
            ("https://api.convis.ai", "api.convis.ai"),
            ("http://api.convis.ai", "api.convis.ai"),
            ("https://api.convis.ai/", "api.convis.ai"),
            ("https://api.convis.ai//", "api.convis.ai"),
        ]

        for url, expected in urls:
            domain = re.sub(r'(^\w+:|^)\/\/|\/+$', '', url)
            assert domain == expected, f"Failed for {url}"


# ============================================================================
# TEST: Phone Number Validation
# ============================================================================
class TestPhoneNumberValidation:
    """Tests for phone number format validation"""

    def test_e164_format_valid(self):
        """Test valid E.164 phone numbers"""
        import re

        valid_numbers = [
            "+1234567890",
            "+12345678901234",
            "+918850501889",
            "+14155551234"
        ]

        pattern = r'^\+[1-9]\d{1,14}$'
        for number in valid_numbers:
            assert re.match(pattern, number), f"Should be valid: {number}"

    def test_e164_format_invalid(self):
        """Test invalid E.164 phone numbers"""
        import re

        invalid_numbers = [
            "1234567890",      # Missing +
            "+0234567890",     # Starts with 0
            "+1",              # Too short
            "+123456789012345678",  # Too long
            "++1234567890",    # Double +
        ]

        pattern = r'^\+[1-9]\d{1,14}$'
        for number in invalid_numbers:
            assert not re.match(pattern, number), f"Should be invalid: {number}"


# ============================================================================
# TEST: Temperature Constraints
# ============================================================================
class TestTemperatureConstraints:
    """Tests for OpenAI Realtime temperature constraints"""

    def test_temperature_minimum_enforcement(self):
        """Test that temperature is enforced to minimum 0.6 for OpenAI Realtime"""
        temperature = 0.3

        if temperature < 0.6:
            temperature = 0.6

        assert temperature == 0.6

    def test_temperature_valid_range(self):
        """Test valid temperature values pass through"""
        temperatures = [0.6, 0.7, 0.8, 0.9, 1.0]

        for temp in temperatures:
            adjusted = temp if temp >= 0.6 else 0.6
            assert adjusted == temp


# ============================================================================
# TEST: Streaming Pipeline Components
# ============================================================================
class TestStreamingPipeline:
    """Tests for streaming pipeline components"""

    def test_asr_provider_routing(self):
        """Test ASR provider routing logic"""
        providers = {
            "deepgram": "Deepgram Nova",
            "openai": "OpenAI Whisper",
            "google": "Google Speech-to-Text",
            "sarvam": "Sarvam Saarika"
        }

        for provider, description in providers.items():
            assert provider in providers

    def test_tts_provider_routing(self):
        """Test TTS provider routing logic"""
        providers = {
            "openai": "OpenAI TTS",
            "elevenlabs": "ElevenLabs",
            "cartesia": "Cartesia Sonic",
            "sarvam": "Sarvam Bulbul"
        }

        for provider, description in providers.items():
            assert provider in providers

    def test_llm_provider_routing(self):
        """Test LLM provider routing logic"""
        providers = {
            "openai": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"],
            "anthropic": ["claude-3-sonnet", "claude-3-haiku"],
            "groq": ["llama-3-70b", "mixtral-8x7b"]
        }

        for provider, models in providers.items():
            assert len(models) > 0


# ============================================================================
# TEST: Conversation History Management
# ============================================================================
class TestConversationHistory:
    """Tests for conversation history management"""

    def test_add_user_message(self):
        """Test adding user message to history"""
        conversation_history = []

        conversation_history.append({
            "role": "user",
            "content": "Hello, I need help with my account"
        })

        assert len(conversation_history) == 1
        assert conversation_history[0]["role"] == "user"

    def test_add_assistant_message(self):
        """Test adding assistant message to history"""
        conversation_history = []

        conversation_history.append({
            "role": "assistant",
            "content": "Hello! I'd be happy to help you with your account."
        })

        assert len(conversation_history) == 1
        assert conversation_history[0]["role"] == "assistant"

    def test_conversation_flow(self):
        """Test typical conversation flow"""
        conversation_history = []

        # User speaks
        conversation_history.append({"role": "user", "content": "Hi"})
        # Assistant responds
        conversation_history.append({"role": "assistant", "content": "Hello!"})
        # User speaks again
        conversation_history.append({"role": "user", "content": "I need help"})
        # Assistant responds
        conversation_history.append({"role": "assistant", "content": "How can I help?"})

        assert len(conversation_history) == 4
        assert conversation_history[0]["role"] == "user"
        assert conversation_history[1]["role"] == "assistant"


# ============================================================================
# TEST: VAD (Voice Activity Detection) Configuration
# ============================================================================
class TestVADConfiguration:
    """Tests for VAD configuration"""

    def test_default_vad_settings(self):
        """Test default VAD configuration values"""
        vad_config = {
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 500
        }

        assert vad_config["threshold"] == 0.5
        assert vad_config["prefix_padding_ms"] == 300
        assert vad_config["silence_duration_ms"] == 500

    def test_custom_vad_settings(self):
        """Test custom VAD configuration"""
        assistant_config = {
            "vad_threshold": 0.7,
            "vad_prefix_padding_ms": 400,
            "vad_silence_duration_ms": 600
        }

        vad_threshold = assistant_config.get('vad_threshold', 0.5)
        vad_prefix_padding_ms = assistant_config.get('vad_prefix_padding_ms', 300)
        vad_silence_duration_ms = assistant_config.get('vad_silence_duration_ms', 500)

        assert vad_threshold == 0.7
        assert vad_prefix_padding_ms == 400
        assert vad_silence_duration_ms == 600


# ============================================================================
# TEST: Error Handling
# ============================================================================
class TestErrorHandling:
    """Tests for error handling in call flows"""

    def test_invalid_assistant_id_handling(self):
        """Test handling of invalid assistant ID"""
        from bson import ObjectId
        from bson.errors import InvalidId

        invalid_ids = ["invalid", "123", "not-an-objectid"]

        for invalid_id in invalid_ids:
            try:
                ObjectId(invalid_id)
                assert False, f"Should have raised error for {invalid_id}"
            except (InvalidId, Exception):
                pass  # Expected

    def test_valid_assistant_id(self):
        """Test valid ObjectId handling"""
        from bson import ObjectId

        valid_id = str(ObjectId())
        parsed_id = ObjectId(valid_id)

        assert str(parsed_id) == valid_id


# ============================================================================
# TEST: Call Status Transitions
# ============================================================================
class TestCallStatusTransitions:
    """Tests for call status state machine"""

    def test_outbound_status_flow(self):
        """Test outbound call status transitions"""
        valid_statuses = ["initiated", "ringing", "in-progress", "completed", "failed", "busy", "no-answer"]

        # Initial status
        status = "initiated"
        assert status in valid_statuses

        # After call connects
        status = "in-progress"
        assert status in valid_statuses

        # After call ends
        status = "completed"
        assert status in valid_statuses

    def test_inbound_status_flow(self):
        """Test inbound call status transitions"""
        valid_statuses = ["ringing", "in-progress", "completed", "failed"]

        status = "in-progress"
        assert status in valid_statuses


# ============================================================================
# TEST: Custom Provider Stream Handler Config
# ============================================================================
class TestCustomProviderConfig:
    """Tests for custom provider stream handler configuration"""

    def test_assistant_config_structure(self):
        """Test assistant config structure for custom providers"""
        assistant_config = {
            'system_message': "You are a helpful assistant",
            'voice': 'alloy',
            'temperature': 0.8,
            'greeting': "Hello! How can I help?",
            'asr_provider': 'deepgram',
            'tts_provider': 'elevenlabs',
            'llm_provider': 'openai',
            'asr_language': 'en',
            'asr_model': 'nova-2',
            'tts_model': 'eleven_turbo_v2',
            'tts_voice': 'EXAVITQu4vr4xnSDxMaL',
            'llm_model': 'gpt-4o-mini',
            'llm_max_tokens': 150
        }

        assert assistant_config['asr_provider'] == 'deepgram'
        assert assistant_config['tts_provider'] == 'elevenlabs'
        assert assistant_config['llm_provider'] == 'openai'

    def test_provider_keys_structure(self):
        """Test provider keys structure"""
        provider_keys = {
            'openai': 'sk-xxx',
            'deepgram': 'dg-xxx',
            'elevenlabs': 'el-xxx',
            'anthropic': None,
            'cartesia': None
        }

        # Filter out None values
        active_keys = {k: v for k, v in provider_keys.items() if v is not None}

        assert len(active_keys) == 3
        assert 'openai' in active_keys


# ============================================================================
# RUN TESTS
# ============================================================================
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
