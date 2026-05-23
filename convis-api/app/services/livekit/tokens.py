"""Mint LiveKit access tokens for browser clients."""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from livekit import api

from app.config.settings import settings


class LiveKitNotConfigured(RuntimeError):
    pass


def _require_credentials() -> tuple[str, str, str]:
    if not (settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret):
        raise LiveKitNotConfigured(
            "LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET must be set"
        )
    return settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret


def mint_participant_token(
    *,
    room_name: str,
    identity: str,
    name: Optional[str] = None,
    metadata: Optional[str] = None,
    ttl_seconds: int = 60 * 60,
) -> str:
    """JWT allowing the browser to join `room_name` as `identity`."""
    _, key, secret = _require_credentials()

    grants = api.VideoGrants(
        room=room_name,
        room_join=True,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )

    token = api.AccessToken(key, secret).with_identity(identity).with_grants(grants)
    if name:
        token = token.with_name(name)
    if metadata:
        token = token.with_metadata(metadata)
    # livekit-api expects a timedelta; older versions accepted int. Pass timedelta.
    token = token.with_ttl(timedelta(seconds=ttl_seconds))
    return token.to_jwt()


def livekit_api_client() -> api.LiveKitAPI:
    url, key, secret = _require_credentials()
    return api.LiveKitAPI(url=url, api_key=key, api_secret=secret)
