from pydantic import BaseModel, Field
from typing import Optional

class OutboundCallRequest(BaseModel):
    """Request model for initiating an outbound call"""
    phone_number: str = Field(..., description="Phone number to call in E.164 format (e.g., +1234567890)")
    from_phone_number_id: Optional[str] = Field(
        None,
        description="Optional Mongo _id of the phone_numbers doc to dial FROM. "
        "Required when an assistant has multiple numbers assigned (one Twilio + one Vobiz, etc) "
        "so the user's provider choice is honored. If omitted, falls back to any number "
        "currently assigned to the assistant.",
    )

class OutboundCallResponse(BaseModel):
    """Response model for outbound call operations"""
    message: str
    call_sid: Optional[str] = None
    status: Optional[str] = None
    assistant_id: Optional[str] = None

class CheckNumberResponse(BaseModel):
    """Response model for phone number validation"""
    phone_number: str
    is_allowed: bool
    message: str

class OutboundCallConfig(BaseModel):
    """Configuration for outbound call handling"""
    assistant_id: str
    system_message: str
    voice: str
    temperature: float
