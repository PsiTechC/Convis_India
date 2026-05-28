from pydantic_settings import BaseSettings
from typing import Optional
import os
import sys
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Print early for Cloud Run debugging
print(f"[SETTINGS] Loading settings module...", flush=True)

# Get the project root directory (two levels up from this file)
BASE_DIR = Path(__file__).resolve().parent.parent.parent
ENV_FILE = BASE_DIR / ".env"

class Settings(BaseSettings):
    # MongoDB Configuration
    mongodb_uri: str
    database_name: str

    # Email Configuration
    email_user: str
    email_pass: str
    smtp_host: str = "p1432.use1.mysecurecloudhost.com"
    smtp_port: int = 465
    smtp_use_ssl: bool = True

    # Application Configuration
    frontend_url: str = "https://webapp.convis.ai"
    # JWT secret has no default — startup fails if not set. This prevents
    # accidental signing of tokens with a constant, repo-known string.
    jwt_secret: str

    # Provider API Keys — full Sarvam stack (Saaras v3 ASR + Sarvam-105b LLM +
    # Bulbul v3 TTS) after the 2026-05 migration. The single SARVAM_API_KEY
    # authenticates all three services against api.sarvam.ai.
    sarvam_api_key: Optional[str] = None
    # OpenAI key is still used by non-voice paths:
    #   - post_call_summary_service.py (transcript summarisation)
    #   - local_embeddings.py / chromadb (RAG vector embeddings)
    #   - realtime_tool_service.py (legacy tool/intent extraction)
    # Migration of those paths to Sarvam is out of scope for the voice swap.
    openai_api_key: Optional[str] = None
    # Deepgram / ElevenLabs / Cartesia keys removed — providers no longer in
    # use after the 2026-05 Sarvam migration. If you redeploy and these env
    # vars are still set they'll be ignored (pydantic-settings extra='ignore').

    # Encryption Configuration (for production)
    encryption_key: Optional[str] = None

    # Environment
    environment: str = "development"  # development, staging, production

    # API Configuration
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_base_url: Optional[str] = None  # For webhook URLs in production
    base_url: Optional[str] = None  # Alias for api_base_url

    # Twilio Configuration (optional defaults)
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None

    # Redis Configuration
    redis_url: str = "redis://localhost:6379"

    # Campaign defaults
    default_timezone: str = "America/New_York"
    default_max_attempts: int = 3

    # Feature flags
    enable_calendar_booking: bool = True
    enable_post_call_ai: bool = True
    enable_auto_retry: bool = True

    # Campaign scheduler (reduced to 1 second for ultra-fast call progression)
    campaign_dispatch_interval_seconds: int = 1

    # LiveKit — media plane for all voice calls (browser + phone via SIP).
    livekit_url: Optional[str] = None  # wss://<project>.livekit.cloud
    livekit_api_key: Optional[str] = None
    livekit_api_secret: Optional[str] = None
    livekit_agent_name: str = "convis-agent"
    # Outbound SIP trunk configured in LiveKit Cloud with Twilio Elastic SIP Trunking
    # termination URI as the transport (created via LiveKit CLI or console).
    livekit_sip_outbound_trunk_id: Optional[str] = None
    # LiveKit SIP ingress hostname for inbound calls (the host Twilio dials into).
    # Example: "<project>-sip.livekit.cloud" — from LiveKit Cloud SIP page.
    livekit_sip_inbound_host: Optional[str] = None

    # Google Calendar
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None

    # Microsoft Calendar
    microsoft_client_id: Optional[str] = None
    microsoft_client_secret: Optional[str] = None
    microsoft_tenant_id: Optional[str] = None
    microsoft_redirect_uri: Optional[str] = None

    model_config = {
        "env_file": str(ENV_FILE),
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore"
    }

    def validate_production_settings(self):
        """Validate critical settings for production environment"""
        if self.environment == "production":
            errors = []

            if not self.encryption_key:
                errors.append("ENCRYPTION_KEY is required for production")

            if not self.openai_api_key:
                logger.warning("OPENAI_API_KEY not set - voice agent will not work")

            if not self.api_base_url:
                errors.append(
                    "API_BASE_URL is required for production "
                    "(Twilio webhook URLs + status callbacks + signature verification)"
                )

            # PSTN call paths need at least one of:
            # (a) LiveKit Outbound SIP Trunk + LiveKit Inbound Host (low-cost path), OR
            # (b) Twilio creds + LiveKit Inbound Host (TwiML fallback path).
            # In production, neither path being usable is a deployment bug.
            has_outbound_sip = bool(self.livekit_sip_outbound_trunk_id)
            has_twilio = bool(self.twilio_account_sid and self.twilio_auth_token)
            has_inbound_host = bool(self.livekit_sip_inbound_host)

            if not has_inbound_host:
                errors.append(
                    "LIVEKIT_SIP_INBOUND_HOST is required for production "
                    "(both inbound PSTN webhooks and Twilio TwiML outbound need it)"
                )
            if not (has_outbound_sip or has_twilio):
                errors.append(
                    "At least one outbound PSTN path must be configured: "
                    "either LIVEKIT_SIP_OUTBOUND_TRUNK_ID or "
                    "TWILIO_ACCOUNT_SID+TWILIO_AUTH_TOKEN"
                )

            if "localhost" in self.frontend_url:
                logger.warning("FRONTEND_URL still set to localhost - update for production")

            if errors:
                raise ValueError(f"Production configuration errors: {', '.join(errors)}")

        logger.info(f"Running in {self.environment} mode")

try:
    settings = Settings()
    print(f"[SETTINGS] Settings loaded successfully. Environment: {settings.environment}", flush=True)
except Exception as e:
    # Do NOT log the env value or even the full env keys list — names alone
    # disclose which secrets are wired (Stripe, OpenAI, JWT, etc.).
    print(f"[SETTINGS] FATAL: Failed to load settings: {e}", flush=True)
    print(
        "[SETTINGS] Required env vars: MONGODB_URI, DATABASE_NAME, EMAIL_USER, "
        "EMAIL_PASS, JWT_SECRET",
        flush=True,
    )
    raise

# Validate settings on startup
try:
    settings.validate_production_settings()
except ValueError as e:
    print(f"[SETTINGS] Configuration validation failed: {e}", flush=True)
    logger.error(f"Configuration validation failed: {e}")
    if settings.environment == "production":
        raise
