"""
Voice library and preferences API routes — ElevenLabs + Cartesia.

`/voices/list?provider=elevenlabs|cartesia` returns the live catalogue from
the selected provider. The frontend's TTS provider toggle calls this endpoint
to populate its voice picker, so any voice the user has in their Cartesia or
ElevenLabs account is selectable — not just a hard-coded subset.
"""
import os
import httpx
from fastapi import APIRouter, Depends, HTTPException, status, Response
from typing import Optional, List
from bson import ObjectId
from datetime import datetime
import logging

from app.models.voice import (
    VoiceMetadata,
    VoiceListResponse,
    SaveVoiceRequest,
    RemoveVoiceRequest,
    UniversalVoiceDemoRequest,
)
from app.config.database import Database
from app.utils.encryption import encryption_service
from app.config.settings import settings
from app.utils.auth import get_current_user

# Every voices route requires a JWT. Without this, /voices/list returned the
# server-side ElevenLabs catalogue to anonymous callers AND burned our paid
# ElevenLabs API quota on every anonymous request — a free DDoS vector.
router = APIRouter(dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)


def _resolve_cartesia_key() -> Optional[str]:
    """Cartesia uses a single account-level key (no per-user keys yet)."""
    return getattr(settings, "cartesia_api_key", None) or os.environ.get("CARTESIA_API_KEY")


def _resolve_elevenlabs_key(user_id: Optional[str]) -> Optional[str]:
    """Look up the user's ElevenLabs API key, falling back to env."""
    if user_id:
        try:
            db = Database.get_db()
            api_keys_collection = db["api_keys"]
            user_obj_id = ObjectId(user_id)
            api_key_doc = api_keys_collection.find_one({
                "user_id": user_obj_id,
                "provider": "custom",
                "$or": [
                    {"label": {"$regex": "eleven", "$options": "i"}},
                    {"description": {"$regex": "eleven", "$options": "i"}},
                ],
            })
            if api_key_doc:
                try:
                    return encryption_service.decrypt(api_key_doc["key"])
                except Exception as e:
                    logger.error(f"Failed to decrypt ElevenLabs key: {e}")
        except Exception as e:
            logger.warning(f"Could not look up user ElevenLabs key: {e}")

    return settings.elevenlabs_api_key


async def fetch_elevenlabs_voices(api_key: str) -> List[VoiceMetadata]:
    """Fetch all voices from ElevenLabs including user's custom voices."""
    voices: List[VoiceMetadata] = []
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                timeout=30.0,
            )
            if response.status_code != 200:
                logger.warning(f"Failed to fetch ElevenLabs voices: {response.status_code}")
                return voices

            data = response.json()
            for voice in data.get("voices", []):
                labels = voice.get("labels", {}) or {}
                gender = labels.get("gender", "neutral")
                if gender not in ["male", "female", "neutral"]:
                    gender = "neutral"
                accent = labels.get("accent") or "American"
                age = labels.get("age", "middle-aged")
                age_group = age.replace(" ", "-") if age in ["young", "middle aged", "old"] else "middle-aged"
                use_case = labels.get("use case") or labels.get("use_case") or "General Purpose"

                voices.append(
                    VoiceMetadata(
                        id=voice.get("voice_id"),
                        name=voice.get("name", "Unknown"),
                        provider="elevenlabs",
                        gender=gender,
                        accent=accent,
                        language="en",
                        description=voice.get("description") or f"{voice.get('name')} - ElevenLabs voice",
                        age_group=age_group if age_group in ["young", "middle-aged", "old"] else "middle-aged",
                        use_case=use_case,
                        model="eleven_turbo_v2_5",
                    )
                )

            logger.info(f"Fetched {len(voices)} voices from ElevenLabs API")
    except Exception as e:
        logger.error(f"Error fetching ElevenLabs voices: {e}")
    return voices


async def fetch_cartesia_voices(api_key: str) -> List[VoiceMetadata]:
    """Fetch the full Cartesia voice library (paginated).

    Cartesia returns:
      {"data": [...10 items...], "next_page": "<token>", "has_more": true}
    We follow pagination until has_more=false. The `gender` field on each
    voice is "feminine" / "masculine" / "neutral" / null — NOT "female"/"male".
    Bug fixed today: prior implementation only fetched the first 10 voices
    AND mis-mapped Cartesia's gender labels so almost everything came back
    as "neutral" in the frontend filter.
    """
    voices: List[VoiceMetadata] = []
    if not api_key:
        return voices
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            url = "https://api.cartesia.ai/voices/"
            params: dict[str, str] = {"limit": "100"}
            page = 0
            while True:
                page += 1
                resp = await client.get(
                    url, params=params,
                    headers={"X-API-Key": api_key, "Cartesia-Version": "2024-11-13"},
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"Cartesia voices page {page} failed: {resp.status_code} {resp.text[:200]}"
                    )
                    break
                data = resp.json()
                items = (data if isinstance(data, list)
                         else (data.get("data") or data.get("voices") or []))
                for v in items:
                    voices.append(_normalize_cartesia_voice(v))
                if not isinstance(data, dict) or not data.get("has_more"):
                    break
                next_token = data.get("next_page")
                if not next_token:
                    break
                params["starting_after"] = str(next_token)
                # Bound paginated walks so a runaway server doesn't infinite-loop.
                if page >= 50:
                    logger.warning("Cartesia pagination capped at 50 pages")
                    break
            logger.info(f"Fetched {len(voices)} voices from Cartesia ({page} page(s))")
    except Exception as e:
        logger.error(f"Error fetching Cartesia voices: {e}")
    return voices


def _normalize_cartesia_voice(v: dict) -> "VoiceMetadata":
    """Map Cartesia's voice schema → our common VoiceMetadata shape.

    Critical mapping detail: Cartesia uses gender = "feminine"/"masculine"/
    "neutral"/null. Frontend filters expect "female"/"male"/"neutral".
    """
    name = v.get("name") or "Unknown"
    desc = v.get("description") or f"{name} - Cartesia voice"
    lang = (v.get("language") or "en").lower()
    raw_gender = (v.get("gender") or "").lower()
    if raw_gender in ("feminine", "female", "f"):
        gender = "female"
    elif raw_gender in ("masculine", "male", "m"):
        gender = "male"
    else:
        gender = "neutral"

    blob = (desc + " " + name).lower()
    accent = "American"
    for hint, label in [
        ("british", "British"), ("english (gb)", "British"), ("uk", "British"),
        ("indian", "Indian"), ("india", "Indian"),
        ("australian", "Australian"), ("aussie", "Australian"),
        ("irish", "Irish"), ("scottish", "Scottish"),
        ("french", "French"), ("german", "German"), ("spanish", "Spanish"),
        ("italian", "Italian"), ("japanese", "Japanese"), ("korean", "Korean"),
        ("mandarin", "Mandarin"), ("russian", "Russian"),
    ]:
        if hint in blob:
            accent = label
            break
    return VoiceMetadata(
        id=v.get("id"),
        name=name,
        provider="cartesia",
        gender=gender,
        accent=accent,
        language=lang,
        description=desc,
        age_group="middle-aged",
        use_case="General Purpose",
        model="sonic-3",
    )


@router.get("/cartesia/sync")
async def sync_cartesia_voices():
    """Fetch the full Cartesia voice library (account-level key)."""
    api_key = _resolve_cartesia_key()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Cartesia API key configured. Set CARTESIA_API_KEY env.",
        )
    voices = await fetch_cartesia_voices(api_key)
    return {
        "success": True,
        "voices": [v.model_dump() for v in voices],
        "total": len(voices),
        "provider": "cartesia",
    }


@router.get("/elevenlabs/sync")
async def sync_elevenlabs_voices(user_id: str):
    """Fetch ElevenLabs voices for the given user (custom + default)."""
    try:
        ObjectId(user_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        )

    api_key = _resolve_elevenlabs_key(user_id)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No ElevenLabs API key found. Add one in Settings or set ELEVENLABS_API_KEY.",
        )

    voices = await fetch_elevenlabs_voices(api_key)
    return {
        "success": True,
        "voices": [v.model_dump() for v in voices],
        "total": len(voices),
        "provider": "elevenlabs",
    }


@router.get("/list", response_model=VoiceListResponse)
async def get_all_voices(
    provider: Optional[str] = None,
    gender: Optional[str] = None,
    accent: Optional[str] = None,
    language: Optional[str] = None,
    user_id: Optional[str] = None,
    include_custom: bool = False,
):
    """List available voices from ElevenLabs, Cartesia, or both with filters.

    - `provider=elevenlabs` (default if omitted) → ElevenLabs catalogue
    - `provider=cartesia` → Cartesia Sonic catalogue
    - `provider=all` → both, concatenated
    """
    selected = (provider or "elevenlabs").lower()
    voices: List[VoiceMetadata] = []
    providers_returned: List[str] = []

    if selected in ("elevenlabs", "all"):
        api_key = _resolve_elevenlabs_key(user_id if include_custom else None)
        if api_key:
            el_voices = await fetch_elevenlabs_voices(api_key)
            voices.extend(el_voices)
            if el_voices:
                providers_returned.append("elevenlabs")

    if selected in ("cartesia", "all"):
        cart_key = _resolve_cartesia_key()
        if cart_key:
            cart_voices = await fetch_cartesia_voices(cart_key)
            voices.extend(cart_voices)
            if cart_voices:
                providers_returned.append("cartesia")

    if gender:
        voices = [v for v in voices if v.gender == gender.lower()]
    if accent:
        voices = [v for v in voices if v.accent.lower() == accent.lower()]
    if language:
        voices = [v for v in voices if v.language == language.lower()]

    return VoiceListResponse(voices=voices, total=len(voices), providers=providers_returned)


@router.get("/preferences/{user_id}")
async def get_user_voice_preferences(user_id: str):
    """Return the user's saved ElevenLabs voice preferences."""
    try:
        ObjectId(user_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        )

    db = Database.get_db()
    user_prefs = db["voice_preferences"].find_one({"user_id": user_id})
    if not user_prefs:
        return {"user_id": user_id, "saved_voices": [], "total": 0}

    saved = [v for v in user_prefs.get("saved_voices", []) if v.get("provider") == "elevenlabs"]
    return {"user_id": user_id, "saved_voices": saved, "total": len(saved)}


@router.post("/preferences/{user_id}/save")
async def save_voice_to_preferences(user_id: str, request: SaveVoiceRequest):
    """Save an ElevenLabs voice to the user's preferences."""
    if request.provider != "elevenlabs":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only 'elevenlabs' voices can be saved.",
        )

    try:
        ObjectId(user_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        )

    db = Database.get_db()
    preferences_collection = db["voice_preferences"]

    voice_entry = {
        "voice_id": request.voice_id,
        "provider": "elevenlabs",
        "nickname": request.nickname,
        "added_at": datetime.utcnow(),
    }

    existing = preferences_collection.find_one({"user_id": user_id})
    if existing:
        preferences_collection.update_one(
            {"user_id": user_id},
            {
                "$pull": {
                    "saved_voices": {
                        "voice_id": request.voice_id,
                        "provider": "elevenlabs",
                    }
                }
            },
        )
        preferences_collection.update_one(
            {"user_id": user_id},
            {
                "$push": {"saved_voices": voice_entry},
                "$set": {"updated_at": datetime.utcnow()},
            },
        )
    else:
        preferences_collection.insert_one(
            {
                "user_id": user_id,
                "saved_voices": [voice_entry],
                "updated_at": datetime.utcnow(),
            }
        )

    return {"success": True, "voice": voice_entry}


@router.post("/preferences/{user_id}/remove")
async def remove_voice_from_preferences(user_id: str, request: RemoveVoiceRequest):
    """Remove a voice from the user's preferences."""
    db = Database.get_db()
    result = db["voice_preferences"].update_one(
        {"user_id": user_id},
        {
            "$pull": {
                "saved_voices": {
                    "voice_id": request.voice_id,
                    "provider": request.provider,
                }
            },
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User preferences not found",
        )
    return {"success": True}


async def _generate_elevenlabs_demo(voice_id: str, model: str, text: str, api_key: str) -> bytes:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                json={
                    "text": text,
                    "model_id": model,
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
                timeout=30.0,
            )
            if response.status_code != 200:
                error_msg = f"ElevenLabs API error: {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = error_data.get("detail", {}).get("message", error_msg)
                except Exception:
                    pass
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=error_msg,
                )
            return response.content
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="ElevenLabs API timeout. Please try again.",
        )


async def _generate_cartesia_demo(voice_id: str, model: str, text: str, api_key: str) -> bytes:
    """Synthesize a short demo via Cartesia /tts/bytes (returns raw audio).

    We request 16-bit PCM @ 22050 Hz wrapped in WAV — every browser plays
    it without a codec fight. Cartesia also supports MP3/OGG via different
    output_format blocks if we need smaller payloads later.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.cartesia.ai/tts/bytes",
                headers={
                    "X-API-Key": api_key,
                    "Cartesia-Version": "2024-11-13",
                    "Content-Type": "application/json",
                },
                json={
                    "model_id": model or "sonic-3",
                    "transcript": text,
                    "voice": {"mode": "id", "id": voice_id},
                    "output_format": {
                        "container": "wav",
                        "encoding": "pcm_s16le",
                        "sample_rate": 22050,
                    },
                    "language": "en",
                },
                timeout=30.0,
            )
            if response.status_code != 200:
                error_msg = f"Cartesia API error: {response.status_code}"
                try:
                    error_msg = response.json().get("error", {}).get("message", error_msg)
                except Exception:
                    error_msg = f"{error_msg}: {response.text[:200]}"
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=error_msg,
                )
            return response.content
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Cartesia API timeout. Please try again.",
        )


@router.post("/demo", status_code=status.HTTP_200_OK)
async def generate_universal_voice_demo(request: UniversalVoiceDemoRequest):
    """Generate a TTS preview for either ElevenLabs OR Cartesia.

    Per-provider:
    - elevenlabs: uses the user's ElevenLabs key (or env fallback) → MP3
    - cartesia: uses the account-level Cartesia key → WAV (PCM 22.05 kHz)
    """
    provider = (request.provider or "elevenlabs").lower()
    try:
        ObjectId(request.user_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user_id format",
        )

    if provider == "cartesia":
        api_key = _resolve_cartesia_key()
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No Cartesia API key configured. Set CARTESIA_API_KEY env.",
            )
        audio_content = await _generate_cartesia_demo(
            request.voice_id, request.model or "sonic-3", request.text, api_key,
        )
        return Response(
            content=audio_content,
            media_type="audio/wav",
            headers={
                "Content-Disposition": f'inline; filename="voice_demo_cartesia_{request.voice_id}.wav"'
            },
        )

    if provider != "elevenlabs":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider {provider!r} not supported for voice demos.",
        )

    api_key = _resolve_elevenlabs_key(request.user_id)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No ElevenLabs API key found. Add one in Settings or set ELEVENLABS_API_KEY.",
        )

    model_to_use = request.model or "eleven_turbo_v2_5"
    audio_content = await _generate_elevenlabs_demo(
        request.voice_id, model_to_use, request.text, api_key
    )

    return Response(
        content=audio_content,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f'inline; filename="voice_demo_elevenlabs_{request.voice_id}.mp3"'
        },
    )
