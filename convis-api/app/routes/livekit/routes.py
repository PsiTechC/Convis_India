"""Browser-facing LiveKit endpoints.

The browser calls POST /api/livekit/token to get:
- livekit_url: the LiveKit Cloud WS URL
- token: a JWT scoped to a newly-created room
- room_name: the room it should join

The backend has already dispatched the agent worker into that room, so as soon
as the browser connects, the agent is there waiting.
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.config.settings import settings
from app.services.livekit.assistant_config import load_assistant_config
from app.services.livekit.sip_service import create_room_with_agent, generate_room_name
from app.services.livekit.tokens import LiveKitNotConfigured, mint_participant_token
from app.utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


class TokenRequest(BaseModel):
    assistant_id: str
    participant_name: Optional[str] = None


class TokenResponse(BaseModel):
    livekit_url: str
    token: str
    room_name: str
    identity: str


@router.get("/")
async def livekit_health():
    return {
        "message": "LiveKit voice service is running",
        "configured": bool(
            settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret
        ),
    }


@router.post("/token", response_model=TokenResponse)
async def issue_browser_token(
    body: TokenRequest,
    current_user: dict = Depends(get_current_user),
) -> TokenResponse:
    """Create a room, dispatch the agent into it, and mint a token the browser
    can use to join.

    Caller must be authenticated and must own the assistant. We never trust a
    client-supplied user_id — it's derived from the JWT.
    """
    try:
        config = load_assistant_config(body.assistant_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    caller_user_id = str(current_user["_id"])
    if str(config.get("user_id")) != caller_user_id:
        # 404 (not 403) to avoid leaking existence of other users' assistants
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Assistant not found: {body.assistant_id}",
        )

    room_name = generate_room_name(prefix="web")
    identity = f"user-{caller_user_id}"

    try:
        await create_room_with_agent(
            room_name=room_name,
            assistant_config=config,
            metadata_extra={"source": "web", "caller_user_id": caller_user_id},
        )
        token = mint_participant_token(
            room_name=room_name,
            identity=identity,
            name=body.participant_name or identity,
        )
    except LiveKitNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        )
    except Exception as exc:
        logger.error("Failed to create LiveKit session", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create LiveKit session: {exc}",
        )

    return TokenResponse(
        livekit_url=settings.livekit_url or "",
        token=token,
        room_name=room_name,
        identity=identity,
    )
