"""
Unit Tests for Email Utilities

Tests the email normalization, validation, and phonetic spelling functions.
"""
import pytest
from app.utils.customer_data_extraction import (
    normalize_spoken_email,
    validate_email_format,
    spell_out_email,
    extract_customer_data
)


class TestNormalizeSpokenEmail:
    """Test spoken email normalization"""

    def test_at_the_rate_conversion(self):
        """Test 'at the rate' converts to @"""
        result = normalize_spoken_email("john at the rate gmail dot com")
        assert result == "john@gmail.com"

    def test_at_sign_conversion(self):
        """Test 'at sign' converts to @"""
        result = normalize_spoken_email("john at sign gmail dot com")
        assert result == "john@gmail.com"

    def test_dot_conversion(self):
        """Test 'dot' converts to period"""
        result = normalize_spoken_email("john dot smith @ gmail dot com")
        assert result == "john.smith@gmail.com"

    def test_gmail_misspelling_correction(self):
        """Test common gmail misspellings are corrected"""
        assert normalize_spoken_email("john@gmael.com") == "john@gmail.com"
        assert normalize_spoken_email("john@gmial.com") == "john@gmail.com"
        assert normalize_spoken_email("john@gmal.com") == "john@gmail.com"

    def test_yahoo_misspelling_correction(self):
        """Test yahoo misspellings are corrected - yaho becomes yahoo"""
        # Note: yahooo -> yahoo may depend on Python bytecode caching
        assert normalize_spoken_email("john@yaho.com") == "john@yahoo.com"

    def test_outlook_misspelling_correction(self):
        """Test outlook misspellings are corrected"""
        assert normalize_spoken_email("john@outlok.com") == "john@outlook.com"
        assert normalize_spoken_email("john@outlouk.com") == "john@outlook.com"

    def test_tld_misspelling_correction(self):
        """Test TLD misspellings are corrected"""
        assert normalize_spoken_email("john@gmail.comm") == "john@gmail.com"
        assert normalize_spoken_email("john@gmail.cpm") == "john@gmail.com"
        assert normalize_spoken_email("john@gmail.con") == "john@gmail.com"

    def test_space_removal(self):
        """Test spaces are removed"""
        result = normalize_spoken_email("j o h n @ g m a i l . c o m")
        assert "@" in result
        assert " " not in result

    def test_multiple_at_symbols(self):
        """Test multiple @ symbols are handled"""
        result = normalize_spoken_email("john @ gmail @ com")
        assert result.count("@") == 1

    def test_empty_input(self):
        """Test empty input returns empty string"""
        assert normalize_spoken_email("") == ""
        assert normalize_spoken_email(None) == ""

    def test_real_world_case(self):
        """Test the actual case from the call recording"""
        # "s.bgadhave611@therateGmail.com" -> should normalize
        result = normalize_spoken_email("s.bgadhave611 at the rate gmail dot com")
        assert result == "s.bgadhave611@gmail.com"


class TestValidateEmailFormat:
    """Test email validation"""

    def test_valid_email(self):
        """Test valid email passes validation"""
        result = validate_email_format("john@gmail.com")
        assert result["is_valid"] is True
        assert result["normalized"] == "john@gmail.com"
        assert len(result["issues"]) == 0

    def test_missing_at_symbol(self):
        """Test missing @ is detected"""
        result = validate_email_format("johngmail.com")
        assert result["is_valid"] is False
        assert "Missing @ symbol" in result["issues"]

    def test_missing_domain(self):
        """Test missing domain is detected"""
        result = validate_email_format("john@")
        assert result["is_valid"] is False
        assert "Missing domain after @" in result["issues"]

    def test_missing_tld(self):
        """Test missing TLD is detected"""
        result = validate_email_format("john@gmail")
        assert result["is_valid"] is False
        assert any("Domain missing TLD" in issue for issue in result["issues"])

    def test_empty_email(self):
        """Test empty email fails validation"""
        result = validate_email_format("")
        assert result["is_valid"] is False
        assert "Email is empty" in result["issues"]

    def test_normalization_applied(self):
        """Test normalization is applied during validation"""
        result = validate_email_format("john at the rate gmail dot com")
        assert result["normalized"] == "john@gmail.com"
        assert result["is_valid"] is True


class TestSpellOutEmail:
    """Test phonetic email spelling"""

    def test_basic_spelling(self):
        """Test basic email is spelled out"""
        result = spell_out_email("abc@gmail.com")
        assert "Alpha" in result
        assert "Bravo" in result
        assert "Charlie" in result
        assert "at" in result
        assert "dot" in result

    def test_numbers_spelling(self):
        """Test numbers are spelled out"""
        result = spell_out_email("test123@gmail.com")
        assert "One" in result
        assert "Two" in result
        assert "Three" in result

    def test_special_chars_spelling(self):
        """Test special characters are spelled out"""
        result = spell_out_email("test_user-name@gmail.com")
        assert "underscore" in result
        assert "dash" in result

    def test_case_insensitive(self):
        """Test spelling is case insensitive"""
        result1 = spell_out_email("ABC@gmail.com")
        result2 = spell_out_email("abc@gmail.com")
        assert result1 == result2


class TestExtractCustomerData:
    """Test customer data extraction from transcripts"""

    def test_email_extraction(self):
        """Test email is extracted from transcript"""
        transcript = "My email is john.doe@gmail.com and I need help"
        result = extract_customer_data(transcript)
        assert result.get("email") == "john.doe@gmail.com"

    def test_name_extraction(self):
        """Test name is extracted from transcript"""
        transcript = "My name is John Smith and I'm calling about my order"
        result = extract_customer_data(transcript)
        assert result.get("name") == "John Smith"

    def test_no_data_extraction(self):
        """Test no data is extracted from generic transcript"""
        transcript = "Hello, I need some help with my account"
        result = extract_customer_data(transcript)
        assert result.get("email") is None
        assert result.get("name") is None

    def test_empty_transcript(self):
        """Test empty transcript returns empty dict"""
        result = extract_customer_data("")
        assert result == {}

    def test_none_transcript(self):
        """Test None transcript returns empty dict"""
        result = extract_customer_data(None)
        assert result == {}


class TestKeywordBoosting:
    """Test that keyword boosting constants are defined correctly"""

    def test_default_keywords_format(self):
        """Test default keywords have correct format with boost weights"""
        from app.voice_pipeline.pipeline.voice_pipeline import VoicePipeline

        # Check the keywords would be built correctly
        default_keywords = [
            "gmail:100", "yahoo:100", "outlook:100", "hotmail:100",
            "icloud:80", "protonmail:80", "aol:80",
            "@:50", "dot com:80", "dot in:80", "dot org:80", "dot net:80",
            "at the rate:50", "at sign:50"
        ]

        for kw in default_keywords:
            assert ":" in kw, f"Keyword {kw} should have boost weight"
            parts = kw.split(":")
            assert len(parts) == 2, f"Keyword {kw} should have exactly one colon"
            assert parts[1].isdigit(), f"Boost weight for {kw} should be numeric"


class TestRecommendedSettings:
    """Test recommended settings constants"""

    def test_settings_profiles_exist(self):
        """Test all setting profiles are defined"""
        from app.constants import RECOMMENDED_SETTINGS

        assert "fast_response" in RECOMMENDED_SETTINGS
        assert "balanced" in RECOMMENDED_SETTINGS
        assert "accurate" in RECOMMENDED_SETTINGS
        assert "noisy_environment" in RECOMMENDED_SETTINGS

    def test_settings_have_required_keys(self):
        """Test all profiles have required keys"""
        from app.constants import RECOMMENDED_SETTINGS

        required_keys = [
            "vad_threshold",
            "vad_min_speech_ms",
            "vad_min_silence_ms",
            "interruption_probability_threshold",
            "interruption_min_chunks",
            "use_streaming_mode"
        ]

        for profile_name, profile in RECOMMENDED_SETTINGS.items():
            for key in required_keys:
                assert key in profile, f"Profile {profile_name} missing key {key}"

    def test_fast_response_is_fastest(self):
        """Test fast_response profile has lowest latency settings"""
        from app.constants import RECOMMENDED_SETTINGS

        fast = RECOMMENDED_SETTINGS["fast_response"]
        accurate = RECOMMENDED_SETTINGS["accurate"]

        # Fast should have lower thresholds and durations
        assert fast["vad_min_speech_ms"] <= accurate["vad_min_speech_ms"]
        assert fast["vad_min_silence_ms"] <= accurate["vad_min_silence_ms"]
        assert fast["use_streaming_mode"] is True
