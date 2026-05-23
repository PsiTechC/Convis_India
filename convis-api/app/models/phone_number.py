from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class PhoneNumberCapabilities(BaseModel):
    voice: bool = True
    sms: bool = True
    mms: bool = False


class ProviderCredentials(BaseModel):
    provider: str = Field(..., description="Provider name (twilio | vobiz)")
    # Twilio API auth (provider == 'twilio'): platform fetches numbers via API
    account_sid: Optional[str] = Field(None, description="Twilio Account SID")
    auth_token: Optional[str] = Field(None, description="Twilio Auth Token")
    # Manual SIP-trunk setup (provider == 'vobiz' or any non-API provider):
    # there is no API to fetch numbers from, so the user supplies the number
    # plus the LiveKit outbound SIP trunk ID we should dial through.
    phone_number: Optional[str] = Field(None, description="E.164 phone number (vobiz)")
    livekit_outbound_trunk_id: Optional[str] = Field(
        None, description="LiveKit outbound SIP trunk ID (vobiz)"
    )
    friendly_name: Optional[str] = Field(None, description="Display name for the number")
    user_id: str = Field(..., description="User ID")
    # Optional: when /connect is called via the new 2-step UX (preview then
    # select), this lists the Twilio number SIDs the user actually wants
    # visible on the dashboard. All other Twilio numbers still get imported
    # (so they're known to the system) but flagged hidden=true.
    # If None, the legacy behavior is preserved (all numbers visible).
    selected_phone_sids: Optional[List[str]] = Field(
        None, description="Twilio number SIDs user picked from the preview list"
    )


class ProviderPreviewRequest(BaseModel):
    """Body for POST /connect/preview — validates creds and returns the list of
    numbers from the provider without persisting anything to MongoDB.

    Frontend uses this in step 1 of the 2-step connect modal to show a checklist
    of numbers the user can opt into.
    """
    provider: str = Field(..., description="twilio")
    account_sid: Optional[str] = Field(None)
    auth_token: Optional[str] = Field(None)


class PreviewedPhoneNumber(BaseModel):
    """One row of the preview list.

    `availability` reflects whether this number can be imported by the caller:
      - "available"     — not yet imported by anyone in Convis
      - "owned_by_self" — already in this user's account (re-sync, no-op)
      - "owned_by_other"— claimed by a different Convis user; cannot import
    The frontend uses this to filter the import checklist to only show what
    the user is allowed to bring into their account.
    """
    sid: str
    phone_number: str
    friendly_name: Optional[str] = None
    capabilities: PhoneNumberCapabilities
    availability: str = "available"
    owner_email: Optional[str] = None  # populated only when owned_by_other


class ProviderPreviewResponse(BaseModel):
    provider: str
    numbers: List[PreviewedPhoneNumber]
    available_count: int = 0
    owned_by_other_count: int = 0
    owned_by_self_count: int = 0


class PhoneNumberResponse(BaseModel):
    id: str
    phone_number: str
    provider: str
    friendly_name: Optional[str] = None
    capabilities: PhoneNumberCapabilities
    status: str
    created_at: str
    assigned_assistant_id: Optional[str] = None
    assigned_assistant_name: Optional[str] = None
    webhook_url: Optional[str] = None
    # When True, hide from the default dashboard list. Toggle via
    # PATCH /phone-numbers/{id}/visibility. Users see a "Show hidden"
    # toggle to reveal them.
    hidden: bool = False


class PhoneNumberListResponse(BaseModel):
    phone_numbers: List[PhoneNumberResponse]
    total: int


class CallLogResponse(BaseModel):
    """Comprehensive call log with all Twilio call information"""
    # Basic Info
    id: str = Field(..., alias="sid", description="Twilio Call SID")
    from_number: str = Field(..., alias="from", description="Caller's phone number")
    to: str = Field(..., description="Recipient's phone number")
    direction: str = Field(..., description="'inbound', 'outbound-api', 'outbound-dial'")

    # Status & Timing
    status: str = Field(..., description="Call status: queued, ringing, in-progress, completed, busy, failed, no-answer, canceled")
    duration: Optional[int] = Field(None, description="Call duration in seconds (null if not completed)")
    start_time: Optional[str] = Field(None, description="When call started")
    end_time: Optional[str] = Field(None, description="When call ended")
    date_created: str = Field(..., description="When call was created")
    date_updated: Optional[str] = Field(None, description="Last update time")

    # Call Quality & Details
    answered_by: Optional[str] = Field(None, description="Who answered: human, machine, fax, unknown")
    caller_name: Optional[str] = Field(None, description="Caller ID name")
    forwarded_from: Optional[str] = Field(None, description="Original number if forwarded")
    parent_call_sid: Optional[str] = Field(None, description="Parent call SID if this is a child call")

    # Pricing & Cost
    price: Optional[str] = Field(None, description="Call cost (negative for charges)")
    price_unit: Optional[str] = Field(None, description="Currency (USD, EUR, etc.)")

    # Recording & Transcription
    recording_url: Optional[str] = Field(None, description="Recording URL if available")
    transcription_text: Optional[str] = Field(None, description="Call transcription if available")
    transcript: Optional[str] = Field(None, description="AI-generated call transcript")
    summary: Optional[str] = Field(None, description="AI-generated call summary")
    sentiment: Optional[str] = Field(None, description="Call sentiment: positive, neutral, negative")
    sentiment_score: Optional[float] = Field(None, description="Sentiment score from -1.0 to 1.0")

    # AI Assistant Info (Custom)
    assistant_id: Optional[str] = Field(None, description="AI assistant that handled the call")
    assistant_name: Optional[str] = Field(None, description="AI assistant name")

    # Queue Info
    queue_time: Optional[str] = Field(None, description="Time spent in queue")

    # Voice Provider Configuration (Custom)
    asr_provider: Optional[str] = Field(None, description="Speech-to-text provider used")
    asr_model: Optional[str] = Field(None, description="ASR model used")
    tts_provider: Optional[str] = Field(None, description="Text-to-speech provider used")
    tts_model: Optional[str] = Field(None, description="TTS model used")
    llm_provider: Optional[str] = Field(None, description="LLM provider used")
    llm_model: Optional[str] = Field(None, description="LLM model used")

    # Calculated Cost Fields
    cost_total: Optional[float] = Field(None, description="Total call cost including API + Twilio")
    cost_api: Optional[float] = Field(None, description="API costs (ASR + LLM + TTS or Realtime API)")
    cost_twilio: Optional[float] = Field(None, description="Twilio costs (calling + recording)")
    cost_currency: Optional[str] = Field(None, description="Currency for costs (USD or INR)")
    cost_calculated: Optional[bool] = Field(None, description="Whether cost has been calculated")
    is_realtime_api: Optional[bool] = Field(None, description="Whether OpenAI Realtime API was used")

    # Customer Data (extracted from conversation)
    customer_data: Optional[Dict[str, str]] = Field(None, description="Customer information extracted from call (name, email, location, appointment)")

    # Structured Conversation Log with timestamps
    conversation_log: Optional[List[Dict[str, Any]]] = Field(
        None,
        description="Structured conversation log with timestamps. Each entry: {role, text, timestamp, elapsed, is_interrupted, text_heard}"
    )

    class Config:
        populate_by_name = True
        from_attributes = True
        json_schema_extra = {
            "example": {
                "id": "CA1234567890abcdef1234567890abcdef",
                "from_number": "+11234567890",
                "to": "+10987654321",
                "direction": "inbound",
                "status": "completed",
                "duration": 120,
                "start_time": "2025-10-14T12:00:00Z",
                "end_time": "2025-10-14T12:02:00Z",
                "date_created": "2025-10-14T11:59:55Z",
                "answered_by": "human",
                "price": "-0.0130",
                "price_unit": "USD",
                "assistant_id": "507f1f77bcf86cd799439011",
                "assistant_name": "Customer Support Bot"
            }
        }


class CallLogListResponse(BaseModel):
    call_logs: List[CallLogResponse]
    total: int


class ConnectProviderResponse(BaseModel):
    message: str
    phone_numbers: List[PhoneNumberResponse]
    provider: str


class AssignAssistantRequest(BaseModel):
    phone_number_id: str = Field(..., description="Phone number ID")
    assistant_id: str = Field(..., description="AI assistant ID to assign")


class AssignAssistantResponse(BaseModel):
    message: str
    phone_number: PhoneNumberResponse
    webhook_configured: bool = False


class ProviderConnectionStatus(BaseModel):
    provider: str
    is_connected: bool
    account_sid: Optional[str] = None
    connected_at: Optional[str] = None


class ProviderConnectionResponse(BaseModel):
    message: str
    connections: List[ProviderConnectionStatus]
