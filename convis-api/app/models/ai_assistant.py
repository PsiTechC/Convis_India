import re
from pydantic import BaseModel, Field, model_validator
from typing import Optional, List
from datetime import datetime

from app.constants import DEFAULT_CALL_GREETING

# E.164 phone number (e.g. +12025550143). Used to validate call_transfer_number.
# Unanchored pattern — always use .fullmatch() (a `$`-anchored pattern with
# re.match would also accept a trailing newline, which we don't want).
_E164_RE = re.compile(r"\+[1-9]\d{1,14}")

class AIAssistantCreate(BaseModel):
    user_id: str
    name: str
    system_message: str
    voice: str = "shubh"
    # 0.4 is the sweet spot for inquiry/info voice bots: consistent enough
    # that the same question gets a similar answer call-to-call (helps prompt
    # caching too), still enough variation to sound natural. 0.7+ produces
    # noticeable inconsistency on a phone call where a single response IS the
    # whole experience. Sales/casual bots may want to bump back up to 0.6-0.7.
    temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    api_key_id: Optional[str] = None  # Reference to stored API key
    openai_api_key: Optional[str] = None  # Legacy direct key support
    call_greeting: Optional[str] = Field(default=None)
    calendar_account_id: Optional[str] = None  # Reference to calendar account for scheduling (deprecated, use calendar_account_ids)
    calendar_account_ids: Optional[List[str]] = []  # Multiple calendar accounts for availability checking and scheduling
    calendar_enabled: Optional[bool] = False  # Enable calendar functionality

    # Voice Mode — always custom (Deepgram + Sarvam LLM + Sarvam Bulbul TTS)
    voice_mode: Optional[str] = "custom"

    # Provider selection — Convis-India Sarvam stack defaults.
    asr_provider: Optional[str] = "deepgram"
    tts_provider: Optional[str] = "sarvam"

    # ASR Configuration
    asr_language: Optional[str] = "en"
    asr_model: Optional[str] = "nova-2-phonecall"
    asr_keywords: Optional[List[str]] = []

    # TTS Configuration
    tts_model: Optional[str] = "bulbul:v3"
    tts_speed: Optional[float] = Field(default=1.0, ge=0.25, le=4.0)
    tts_voice: Optional[str] = "shubh"
    # Cartesia-only knobs (ignored when tts_provider != "cartesia"). Validated /
    # whitelisted server-side by assistant_config._coerce_tts_emotion /
    # _coerce_tts_language — keep these permissive at the schema layer so the
    # backend coercer is the single source of truth.
    tts_language: Optional[str] = "en"  # BCP-47 short code; "en", "es", "fr", …
    # Sonic-3 takes a SINGLE Title-Case emotion (Happy / Curious / Determined / …).
    # Stored as a list for Mongo / forward-compat (future multi-emotion API), but
    # coercion always normalizes to [] or a 1-element list.
    tts_emotion: Optional[List[str]] = []
    # Expressive mode: opt-in. Forces ElevenLabs v3 (renders [laughs]/[sighs]/
    # [coughs] inline) and tells the LLM to emit emotion cues. Higher TTFB
    # (~400-700ms vs ~150ms on Flash), so off by default.
    expressive_mode: Optional[bool] = False
    # Multilingual mode: opt-in. ASR runs in language=multi (auto-detect), LLM
    # is told to match the caller's language. ElevenLabs Flash already speaks
    # 32 languages from one voice, so no TTS change. Off by default — enabling
    # trades a small bit of English-PSTN accuracy for cross-language support.
    multilingual: Optional[bool] = False

    # Transcription & Interruptions
    enable_precise_transcript: Optional[bool] = False  # Generate more precise transcripts during interruptions
    interruption_threshold: Optional[int] = Field(default=2, ge=1, le=10)  # Number of words before allowing interruption

    # Voice Response Rate
    response_rate: Optional[str] = "balanced"  # rapid, balanced, relaxed

    # User Online Detection
    check_user_online: Optional[bool] = True  # Check if user is online during call

    # Buffer & Latency Settings
    audio_buffer_size: Optional[int] = Field(default=200, ge=50, le=1000)  # Audio buffer size in ms

    # LLM Configuration — Convis-India Sarvam stack defaults.
    #   sarvam-105b: flagship Indic-tuned MoE; agent injects "/nothink" at the
    #     start of the system prompt to disable reasoning and keep TTFT
    #     under ~3s. Don't remove /nothink without re-benchmarking.
    #   max_tokens=250: ~70s of speech, prevents mid-sentence cutoffs.
    llm_provider: Optional[str] = "sarvam"
    llm_model: Optional[str] = "sarvam-105b"
    llm_max_tokens: Optional[int] = Field(default=250, ge=50, le=4000)

    # Language Configuration
    bot_language: Optional[str] = "en"  # Language for bot responses (en, hi, es, fr, de, etc.)

    # Noise Suppression & Voice Activity Detection (VAD)
    noise_suppression_level: Optional[str] = "medium"  # off, low, medium, high, maximum
    vad_threshold: Optional[float] = Field(default=0.4, ge=0.0, le=1.0)  # Voice activity detection threshold (0.0-1.0)
    vad_prefix_padding_ms: Optional[int] = Field(default=300, ge=0, le=1000)  # Padding before speech starts (ms)
    vad_silence_duration_ms: Optional[int] = Field(default=500, ge=100, le=2000)  # Silence duration to detect end of speech (ms)
    vad_min_speech_ms: Optional[int] = Field(default=150, ge=50, le=1000)  # Minimum speech segment length (ms)
    vad_min_silence_ms: Optional[int] = Field(default=200, ge=50, le=1000)  # Minimum silence to mark end of speech (ms)

    # Real-time Interruption & Streaming Mode
    enable_interruption: Optional[bool] = True  # Enable real-time interruption when user speaks
    interruption_probability_threshold: Optional[float] = Field(default=0.6, ge=0.0, le=1.0)  # Speech probability to trigger interruption
    interruption_min_chunks: Optional[int] = Field(default=2, ge=1, le=10)  # Consecutive speech chunks needed to confirm interruption
    use_streaming_mode: Optional[bool] = True  # Stream LLM response sentence-by-sentence for lower latency

    # Workflow Integration - Post-call automation
    assigned_workflows: Optional[List[str]] = []  # List of workflow IDs assigned to this assistant
    workflow_trigger_events: Optional[List[str]] = ["CALL_COMPLETED"]  # Events that trigger workflows

    # Real-time Tool Calling (Vapi-like functionality) - Tools that run DURING the call
    tools_enabled: Optional[bool] = False  # Enable real-time tool calling during calls
    tools: Optional[List[dict]] = []  # List of tool definitions (webhooks, functions, etc.)
    max_tool_calls_per_turn: Optional[int] = Field(default=5, ge=1, le=20)  # Max tools per conversation turn
    tool_execution_timeout: Optional[int] = Field(default=30, ge=5, le=120)  # Timeout for tool execution in seconds

    # Background Audio Configuration
    background_audio_enabled: Optional[bool] = False  # Enable background audio during calls
    background_audio_type: Optional[str] = "custom"  # custom, call_center, office, cafe, white_noise
    background_audio_volume: Optional[float] = Field(default=0.25, ge=0.0, le=1.0)  # Volume level (0.0-1.0) - 25% default

    # Call Transfer to a Human Agent — opt-in. When enabled (with a valid E.164
    # number) the AI gets a transfer_to_agent tool and a prompt instruction to
    # escalate to a human if it can't help or the caller asks for a person.
    call_transfer_enabled: Optional[bool] = False
    call_transfer_number: Optional[str] = None  # E.164, required when call_transfer_enabled
    call_transfer_message: Optional[str] = None  # What the AI says while connecting; blank → default
    call_transfer_conditions: Optional[str] = Field(default=None, max_length=500)  # Extra "when to transfer" notes

    # Conversation memory across calls — opt-in. When enabled, every completed
    # call generates a structured summary stored in `call_summaries`, and the
    # NEXT call to the same phone number injects the last N summaries into a
    # SECOND system message (separate from the main system prompt, so OpenAI
    # prompt cache hits on the base prompt are preserved). See P2 design in
    # services/post_call_summary_service.py and services/conversation_history_service.py.
    # Default OFF — zero impact on existing assistants until operator flips it on.
    conversation_history_enabled: Optional[bool] = False
    conversation_history_max_calls: Optional[int] = Field(
        default=3, ge=1, le=10,
        description="Number of most-recent summaries to inject into the next call's context. 1-10."
    )

    @model_validator(mode="after")
    def _validate_call_transfer(self):
        if self.call_transfer_enabled:
            n = (self.call_transfer_number or "").strip()
            if not _E164_RE.fullmatch(n):
                raise ValueError("call_transfer_number must be E.164 (e.g. +12025550143) when call transfer is enabled")
        return self


class AIAssistantUpdate(BaseModel):
    name: Optional[str] = None
    system_message: Optional[str] = None
    voice: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    api_key_id: Optional[str] = None  # Update to a stored API key
    openai_api_key: Optional[str] = None  # Legacy direct key update
    call_greeting: Optional[str] = Field(default=None)
    calendar_account_id: Optional[str] = None  # Update calendar account reference (deprecated, use calendar_account_ids)
    calendar_account_ids: Optional[List[str]] = None  # Update multiple calendar accounts
    calendar_enabled: Optional[bool] = None  # Enable/disable calendar functionality

    # Voice Mode Selection
    voice_mode: Optional[str] = None  # realtime or custom

    # Provider selection (only used in custom mode)
    asr_provider: Optional[str] = None  # openai, deepgram, groq
    tts_provider: Optional[str] = None  # openai, cartesia, elevenlabs

    # ASR Configuration
    asr_language: Optional[str] = None
    asr_model: Optional[str] = None
    asr_keywords: Optional[List[str]] = None

    # TTS Configuration
    tts_model: Optional[str] = None
    tts_speed: Optional[float] = Field(default=None, ge=0.25, le=4.0)
    tts_voice: Optional[str] = None
    # Cartesia-only — see AIAssistantCreate. Optional[...] = None so an Update
    # request omitting them is a no-op (existing values preserved).
    tts_language: Optional[str] = None
    tts_emotion: Optional[List[str]] = None
    expressive_mode: Optional[bool] = None  # see AIAssistantCreate
    multilingual: Optional[bool] = None  # see AIAssistantCreate

    # Transcription & Interruptions
    enable_precise_transcript: Optional[bool] = None
    interruption_threshold: Optional[int] = Field(default=None, ge=1, le=10)

    # Voice Response Rate
    response_rate: Optional[str] = None  # rapid, balanced, relaxed

    # User Online Detection
    check_user_online: Optional[bool] = None

    # Buffer & Latency Settings
    audio_buffer_size: Optional[int] = Field(default=None, ge=50, le=1000)

    # LLM Configuration
    llm_provider: Optional[str] = None  # openai, azure, openrouter, deepseek, anthropic, groq, ollama
    llm_model: Optional[str] = None  # e.g., gpt-4.1-mini, gpt-4o, llama3.2, mistral, phi3, etc.
    llm_max_tokens: Optional[int] = Field(default=None, ge=50, le=4000)  # Max tokens in response

    # Language Configuration
    bot_language: Optional[str] = None  # Language for bot responses

    # Noise Suppression & Voice Activity Detection (VAD)
    noise_suppression_level: Optional[str] = None  # off, low, medium, high, maximum
    vad_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # Voice activity detection threshold
    vad_prefix_padding_ms: Optional[int] = Field(default=None, ge=0, le=1000)  # Padding before speech starts
    vad_silence_duration_ms: Optional[int] = Field(default=None, ge=100, le=2000)  # Silence duration to detect end of speech
    vad_min_speech_ms: Optional[int] = Field(default=None, ge=50, le=1000)  # Minimum speech segment length (ms)
    vad_min_silence_ms: Optional[int] = Field(default=None, ge=50, le=1000)  # Minimum silence to mark end of speech (ms)

    # Real-time Interruption & Streaming Mode
    enable_interruption: Optional[bool] = None  # Enable real-time interruption when user speaks
    interruption_probability_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # Speech probability to trigger interruption
    interruption_min_chunks: Optional[int] = Field(default=None, ge=1, le=10)  # Consecutive speech chunks needed
    use_streaming_mode: Optional[bool] = None  # Stream LLM response sentence-by-sentence

    # Workflow Integration - Post-call automation
    assigned_workflows: Optional[List[str]] = None  # List of workflow IDs assigned to this assistant
    workflow_trigger_events: Optional[List[str]] = None  # Events that trigger workflows

    # Real-time Tool Calling (Vapi-like functionality) - Tools that run DURING the call
    tools_enabled: Optional[bool] = None  # Enable real-time tool calling during calls
    tools: Optional[List[dict]] = None  # List of tool definitions (webhooks, functions, etc.)
    max_tool_calls_per_turn: Optional[int] = Field(default=None, ge=1, le=20)  # Max tools per conversation turn
    tool_execution_timeout: Optional[int] = Field(default=None, ge=5, le=120)  # Timeout for tool execution

    # Background Audio Configuration
    background_audio_enabled: Optional[bool] = None  # Enable background audio during calls
    background_audio_type: Optional[str] = None  # custom, call_center, office, cafe, white_noise
    background_audio_volume: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # Volume level (0.0-1.0)

    # Call Transfer to a Human Agent — see AIAssistantCreate
    call_transfer_enabled: Optional[bool] = None
    call_transfer_number: Optional[str] = None  # E.164
    call_transfer_message: Optional[str] = None
    call_transfer_conditions: Optional[str] = Field(default=None, max_length=500)

    # Conversation memory across calls — see AIAssistantCreate.
    conversation_history_enabled: Optional[bool] = None
    conversation_history_max_calls: Optional[int] = Field(default=None, ge=1, le=10)

    @model_validator(mode="after")
    def _validate_call_transfer(self):
        # On a PATCH, only validate the FORMAT of a number that's actually
        # present in this request. The "enabled requires *some* number" check
        # (which can be satisfied by a number already on the doc) lives at the
        # route level — see routes/ai_assistant/assistants.py.
        n = (self.call_transfer_number or "").strip()
        if n and not _E164_RE.fullmatch(n):
            raise ValueError("call_transfer_number must be E.164 (e.g. +12025550143)")
        # Explicitly enabling AND explicitly clearing the number in the same
        # request is contradictory — reject it rather than silently falling
        # back to a stale stored number at the route layer.
        if self.call_transfer_enabled is True and self.call_transfer_number is not None and not n:
            raise ValueError("call_transfer_number is required when enabling call transfer")
        return self


class KnowledgeBaseFile(BaseModel):
    filename: str
    file_type: str
    file_size: int
    uploaded_at: str
    file_path: str


class EmailAttachment(BaseModel):
    """Model for email attachments stored per assistant"""
    filename: str
    original_filename: str
    file_type: str
    file_size: int
    uploaded_at: str
    file_path: str


class SmtpConfig(BaseModel):
    """SMTP configuration for sending emails"""
    enabled: bool = False
    sender_email: str = ""
    sender_name: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""  # Will be encrypted before storage
    use_tls: bool = True
    use_ssl: bool = False


class EmailTemplateConfig(BaseModel):
    """Email template configuration"""
    enabled: bool = False
    logo_url: Optional[str] = None  # URL or base64 of logo image
    subject_template: str = "Your Appointment Confirmation - {{appointment_date}}"
    body_html: str = ""  # Rich HTML content from TipTap editor
    body_text: str = ""  # Plain text fallback
    # Template variables available:
    # {{customer_name}}, {{customer_email}}, {{customer_phone}}
    # {{appointment_date}}, {{appointment_time}}, {{appointment_duration}}
    # {{appointment_title}}, {{meeting_link}}, {{location}}
    # {{company_name}}, {{agent_name}}, {{sender_name}}

class DatabaseConfig(BaseModel):
    enabled: bool = False
    type: str = "postgresql"  # postgresql, mysql, mongodb
    host: str = ""
    port: str = "5432"
    database: str = ""
    username: str = ""
    password: str = ""
    table_name: str = ""
    search_columns: List[str] = []

class AIAssistantResponse(BaseModel):
    id: str
    user_id: str
    name: str
    system_message: str
    voice: str
    temperature: float
    call_greeting: str = Field(default=DEFAULT_CALL_GREETING)
    has_api_key: bool  # Indicates if OpenAI API key is configured
    api_key_id: Optional[str] = None
    api_key_label: Optional[str] = None
    api_key_provider: Optional[str] = None
    knowledge_base_files: List[KnowledgeBaseFile] = []
    has_knowledge_base: bool = False
    database_config: Optional[DatabaseConfig] = None
    calendar_account_id: Optional[str] = None  # Linked calendar account for scheduling (deprecated)
    calendar_account_email: Optional[str] = None  # Email of linked calendar for display (deprecated)
    calendar_account_ids: List[str] = []  # Multiple linked calendar accounts
    calendar_enabled: bool = False  # Calendar functionality enabled
    last_calendar_used_index: int = -1  # For round-robin scheduling

    # Voice Mode — always custom
    voice_mode: str = "custom"

    # Provider selection — Convis-India Sarvam stack.
    asr_provider: str = "deepgram"
    tts_provider: str = "sarvam"

    # ASR Configuration
    asr_language: str = "en"
    asr_model: Optional[str] = "nova-2-phonecall"
    asr_keywords: List[str] = []

    # TTS Configuration
    tts_model: Optional[str] = "bulbul:v3"
    tts_speed: float = 1.0
    tts_voice: Optional[str] = "shubh"
    # Cartesia-only knobs (ignored on ElevenLabs assistants) — see AIAssistantCreate.
    tts_language: str = "en"
    tts_emotion: List[str] = []
    expressive_mode: bool = False  # see AIAssistantCreate
    multilingual: bool = False  # see AIAssistantCreate

    # Transcription & Interruptions
    enable_precise_transcript: bool = False
    interruption_threshold: int = 2

    # Voice Response Rate
    response_rate: str = "balanced"

    # User Online Detection
    check_user_online: bool = True

    # Buffer & Latency Settings
    audio_buffer_size: int = 200

    # LLM Configuration — see AIAssistantCreate for rationale on the defaults
    llm_provider: str = "sarvam"
    llm_model: Optional[str] = "sarvam-105b"
    llm_max_tokens: int = 250

    # Language Configuration
    bot_language: str = "en"  # Language for bot responses

    # Noise Suppression & Voice Activity Detection (VAD)
    noise_suppression_level: str = "medium"  # off, low, medium, high, maximum
    vad_threshold: float = 0.4  # Voice activity detection threshold
    vad_prefix_padding_ms: int = 300  # Padding before speech starts
    vad_silence_duration_ms: int = 500  # Silence duration to detect end of speech
    vad_min_speech_ms: int = 150  # Minimum speech segment length (ms)
    vad_min_silence_ms: int = 200  # Minimum silence to mark end of speech (ms)

    # Real-time Interruption & Streaming Mode
    enable_interruption: bool = True  # Enable real-time interruption when user speaks
    interruption_probability_threshold: float = 0.6  # Speech probability to trigger interruption
    interruption_min_chunks: int = 2  # Consecutive speech chunks needed to confirm interruption
    use_streaming_mode: bool = True  # Stream LLM response sentence-by-sentence for lower latency

    # Workflow Integration - Post-call automation
    assigned_workflows: List[str] = []  # List of workflow IDs assigned to this assistant
    workflow_trigger_events: List[str] = ["CALL_COMPLETED"]  # Events that trigger workflows

    # Real-time Tool Calling (Vapi-like functionality) - Tools that run DURING the call
    tools_enabled: bool = False  # Enable real-time tool calling during calls
    tools: List[dict] = []  # List of tool definitions (webhooks, functions, etc.)
    max_tool_calls_per_turn: int = 5  # Max tools per conversation turn
    tool_execution_timeout: int = 30  # Timeout for tool execution in seconds

    # Background Audio Configuration
    background_audio_enabled: bool = False  # Enable background audio during calls
    background_audio_type: str = "custom"  # custom, call_center, office, cafe, white_noise
    background_audio_volume: float = 0.25  # Volume level (0.0-1.0) - 25% default

    # Call Transfer to a Human Agent
    call_transfer_enabled: bool = False
    call_transfer_number: Optional[str] = None
    call_transfer_message: Optional[str] = None
    call_transfer_conditions: Optional[str] = None

    # Conversation memory across calls
    conversation_history_enabled: bool = False
    conversation_history_max_calls: int = 3

    created_at: str
    updated_at: str


class AIAssistantListResponse(BaseModel):
    assistants: list[AIAssistantResponse]
    total: int

class DeleteResponse(BaseModel):
    message: str

class FileUploadResponse(BaseModel):
    message: str
    file: KnowledgeBaseFile
    total_files: int

class DatabaseConnectionTestRequest(BaseModel):
    enabled: bool
    type: str
    host: str
    port: str
    database: str
    username: str
    password: str
    table_name: str
    search_columns: List[str]

class DatabaseConnectionTestResponse(BaseModel):
    success: bool
    message: str
    record_count: Optional[int] = None


# Email-related request/response models
class SmtpTestRequest(BaseModel):
    """Request model for testing SMTP connection"""
    sender_email: str
    sender_name: str
    smtp_host: str
    smtp_port: int = 587
    smtp_username: str
    smtp_password: str
    use_tls: bool = True
    use_ssl: bool = False
    test_recipient: str  # Email to send test to


class SmtpTestResponse(BaseModel):
    """Response for SMTP connection test"""
    success: bool
    message: str


class EmailAttachmentUploadResponse(BaseModel):
    """Response for email attachment upload"""
    message: str
    attachment: EmailAttachment
    total_attachments: int


class SendTestEmailRequest(BaseModel):
    """Request to send a test email with current template"""
    test_recipient: str  # Email to send test to


class EmailLogEntry(BaseModel):
    """Model for tracking sent emails"""
    id: str
    assistant_id: str
    user_id: str
    recipient_email: str
    recipient_name: Optional[str] = None
    subject: str
    status: str  # sent, failed, bounced, opened, clicked
    appointment_id: Optional[str] = None
    call_sid: Optional[str] = None
    error_message: Optional[str] = None
    sent_at: str
    opened_at: Optional[str] = None
    clicked_at: Optional[str] = None
