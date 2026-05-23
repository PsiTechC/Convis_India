from fastapi import APIRouter, Depends, HTTPException, status, Request
from app.models.phone_number import (
    ProviderCredentials,
    PhoneNumberResponse,
    PhoneNumberListResponse,
    CallLogResponse,
    CallLogListResponse,
    ConnectProviderResponse,
    PhoneNumberCapabilities,
    AssignAssistantRequest,
    AssignAssistantResponse,
    ProviderConnectionStatus,
    ProviderConnectionResponse,
    ProviderPreviewRequest,
    ProviderPreviewResponse,
    PreviewedPhoneNumber,
)
from app.config.database import Database
from app.config.settings import settings
from app.utils.auth import get_current_user, verify_user_ownership
from app.utils.encryption import encryption_service
from app.utils.twilio_helpers import decrypt_twilio_credentials, CredentialDecryptionError
from app.utils.pricing import PricingCalculator
from app.utils.customer_data_extraction import extract_customer_data
from bson import ObjectId
from datetime import datetime
from typing import Any, List, Optional, Set
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
import httpx
import logging
import re

logger = logging.getLogger(__name__)


# Router-level auth: every phone-numbers endpoint requires JWT. Phone numbers
# are PII (FROM/TO leaks); call logs hold PII + system prompts. Anonymous
# access is a Blocker.
PHONE_AUTH = [Depends(get_current_user)]

router = APIRouter(dependencies=PHONE_AUTH)


def _serialize_datetime(value: Any, *, fallback: bool = False) -> Optional[str]:
    """
    Coerce datetime-like values (datetime, str, None) into ISO strings.

    Args:
        value: Raw value from MongoDB/Twilio response.
        fallback: Whether to return a current timestamp when the value is empty.

    Returns:
        ISO8601 datetime string or None.
    """
    if value is None:
        return datetime.utcnow().isoformat() + "Z" if fallback else None

    if isinstance(value, datetime):
        iso_value = value.isoformat()
        return iso_value if value.tzinfo else f"{iso_value}Z"

    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
        return datetime.utcnow().isoformat() + "Z" if fallback else None

    return datetime.utcnow().isoformat() + "Z" if fallback else None


@router.get("/connection-status/{user_id}", response_model=ProviderConnectionResponse, status_code=status.HTTP_200_OK)
async def get_provider_connection_status(user_id: str):
    """
    Check if user has any provider connections established

    Args:
        user_id: User ID

    Returns:
        ProviderConnectionResponse: List of connected providers

    Raises:
        HTTPException: If user not found or error occurs
    """
    try:
        db = Database.get_db()
        users_collection = db['users']
        provider_connections_collection = db['provider_connections']

        logger.info(f"Checking provider connections for user: {user_id}")

        # Verify user exists
        try:
            user_obj_id = ObjectId(user_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user_id format"
            )

        user = users_collection.find_one({"_id": user_obj_id})
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Find all provider connections for this user
        connections = list(provider_connections_collection.find({"user_id": user_obj_id}))

        connection_statuses = []
        for conn in connections:
            connection_statuses.append(ProviderConnectionStatus(
                provider=conn["provider"],
                is_connected=conn.get("status") == "active",
                account_sid=conn.get("account_sid", "")[:8] + "..." if conn.get("account_sid") else None,  # Masked
                connected_at=conn["created_at"].isoformat() + "Z" if conn.get("created_at") else None
            ))

        # If no connections, return empty list
        if not connection_statuses:
            return ProviderConnectionResponse(
                message="No provider connections found",
                connections=[]
            )

        return ProviderConnectionResponse(
            message=f"Found {len(connection_statuses)} provider connection(s)",
            connections=connection_statuses
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking provider connections: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error checking provider connections: {str(e)}"
        )


@router.post("/connect/preview", response_model=ProviderPreviewResponse, status_code=status.HTTP_200_OK)
async def preview_provider_numbers(
    payload: ProviderPreviewRequest,
    current_user: dict = Depends(get_current_user),
):
    """Validate provider credentials and list available numbers WITHOUT
    persisting anything. The frontend uses this to render a selection
    checklist before calling /connect with `selected_phone_sids`.

    Auth: JWT required (preview is a credential test, but only authenticated
    users get to probe Twilio APIs through us).
    """
    if (payload.provider or "").lower() != "twilio":
        raise HTTPException(status_code=400, detail="Only twilio is supported for preview")
    if not payload.account_sid or not payload.account_sid.startswith("AC"):
        raise HTTPException(status_code=400, detail="Invalid Account SID format. Must start with 'AC'.")
    if not payload.auth_token or len(payload.auth_token) < 20:
        raise HTTPException(status_code=400, detail="Invalid Auth Token format")
    try:
        client = Client(payload.account_sid.strip(), payload.auth_token.strip())
        numbers = client.incoming_phone_numbers.list(limit=200)

        # Annotate each Twilio number with its current ownership in Convis so
        # the frontend can filter to only what's importable. Single-owner
        # invariant means we just look up by provider_sid (unique index since
        # the tenant-isolation fix). Cross-checking against the JWT user_id
        # distinguishes "already mine, re-sync no-op" from "another user owns
        # this — refuse to clobber".
        try:
            jwt_user_obj_id = ObjectId(current_user["user_id"])
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid user")

        db = Database.get_db()
        sids = [n.sid for n in numbers]
        # One round-trip to Mongo for all SIDs; build a sid → owner_user_id map.
        owner_by_sid = {}
        for d in db["phone_numbers"].find(
            {"provider_sid": {"$in": sids}},
            {"provider_sid": 1, "user_id": 1},
        ):
            owner_by_sid[d["provider_sid"]] = d.get("user_id")

        # Lookup owner emails in bulk (only for foreign owners, to label them).
        foreign_owner_ids = {
            uid for uid in owner_by_sid.values()
            if uid is not None and uid != jwt_user_obj_id
        }
        email_by_uid = {}
        if foreign_owner_ids:
            for u in db["users"].find(
                {"_id": {"$in": list(foreign_owner_ids)}},
                {"email": 1},
            ):
                email_by_uid[u["_id"]] = u.get("email")

        previewed: list[PreviewedPhoneNumber] = []
        avail_cnt = self_cnt = other_cnt = 0
        for n in numbers:
            owner_uid = owner_by_sid.get(n.sid)
            if owner_uid is None:
                avail = "available"; avail_cnt += 1
                owner_email = None
            elif owner_uid == jwt_user_obj_id:
                avail = "owned_by_self"; self_cnt += 1
                owner_email = None
            else:
                avail = "owned_by_other"; other_cnt += 1
                owner_email = email_by_uid.get(owner_uid)
            previewed.append(PreviewedPhoneNumber(
                sid=n.sid,
                phone_number=n.phone_number,
                friendly_name=n.friendly_name or n.phone_number,
                capabilities=PhoneNumberCapabilities(
                    voice=n.capabilities.get("voice", False),
                    sms=n.capabilities.get("sms", False),
                    mms=n.capabilities.get("mms", False),
                ),
                availability=avail,
                owner_email=owner_email,
            ))
        return ProviderPreviewResponse(
            provider="twilio",
            numbers=previewed,
            available_count=avail_cnt,
            owned_by_self_count=self_cnt,
            owned_by_other_count=other_cnt,
        )
    except TwilioRestException as e:
        if e.status == 401 or e.code == 20003:
            raise HTTPException(status_code=401, detail="Twilio authentication failed. Check your Account SID and Auth Token.")
        raise HTTPException(status_code=400, detail=f"Twilio API error: {e}")
    except Exception as e:
        logger.exception("Preview failed")
        raise HTTPException(status_code=500, detail=f"Failed to preview numbers: {e}")


@router.post("/connect", response_model=ConnectProviderResponse, status_code=status.HTTP_200_OK)
async def connect_provider(
    credentials: ProviderCredentials,
    current_user: dict = Depends(get_current_user),
):
    """
    Connect a telephony provider and sync phone numbers.

    Auth: JWT required. The user_id is taken from the JWT subject — we
    deliberately IGNORE `credentials.user_id` from the body, because trusting
    body-provided user_id was an IDOR letting authenticated user A plant
    numbers under user B's account.
    """
    try:
        db = Database.get_db()
        users_collection = db['users']
        phone_numbers_collection = db['phone_numbers']
        provider_connections_collection = db['provider_connections']

        # Authoritative user identity = JWT subject. Body's user_id is ignored
        # for tenant assignment but we still log a warning when it disagrees,
        # so we can spot client bugs / cross-account probing in the field.
        try:
            user_obj_id = ObjectId(current_user["user_id"])
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid user")
        if credentials.user_id and credentials.user_id != current_user["user_id"]:
            logger.warning(
                "[CONNECT] body.user_id=%s != jwt.user_id=%s — ignoring body",
                credentials.user_id, current_user["user_id"],
            )

        logger.info(f"Connecting {credentials.provider} for user: {current_user['user_id']}")

        user = users_collection.find_one({"_id": user_obj_id})
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        phone_numbers = []

        # Handle Twilio provider
        if credentials.provider.lower() == "twilio":
            try:
                # Log credential format (not the actual values for security)
                logger.info(f"[TWILIO_CONNECT] Account SID length: {len(credentials.account_sid) if credentials.account_sid else 0}")
                logger.info(f"[TWILIO_CONNECT] Account SID starts with: {credentials.account_sid[:4] if credentials.account_sid and len(credentials.account_sid) > 4 else 'N/A'}")
                logger.info(f"[TWILIO_CONNECT] Auth Token length: {len(credentials.auth_token) if credentials.auth_token else 0}")

                # Validate credential format
                if not credentials.account_sid or not credentials.account_sid.startswith('AC'):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid Account SID format. It should start with 'AC'."
                    )

                if not credentials.auth_token or len(credentials.auth_token) < 20:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid Auth Token format. Please copy the full Auth Token from Twilio Console."
                    )

                # Initialize Twilio client
                client = Client(credentials.account_sid.strip(), credentials.auth_token.strip())

                # Test connection and fetch phone numbers
                incoming_phone_numbers = client.incoming_phone_numbers.list(limit=50)

                logger.info(f"Found {len(incoming_phone_numbers)} phone numbers from Twilio")

                # Store or update provider connection
                now = datetime.utcnow()
                encrypted_sid = encryption_service.encrypt(credentials.account_sid)
                encrypted_token = encryption_service.encrypt(credentials.auth_token)

                provider_connection = {
                    "user_id": user_obj_id,
                    "provider": "twilio",
                    "account_sid": encrypted_sid,
                    "auth_token": encrypted_token,
                    "account_sid_last4": credentials.account_sid[-4:],
                    "status": "active",
                    "created_at": now,
                    "updated_at": now
                }

                # Upsert provider connection
                provider_connections_collection.update_one(
                    {"user_id": user_obj_id, "provider": "twilio"},
                    {"$set": provider_connection},
                    upsert=True
                )

                # If the frontend passed `selected_phone_sids`, only those land
                # visible on the dashboard — every other number still gets
                # imported (so the system knows about it for assignment / SIP
                # trunk lookups) but flagged hidden=True. Legacy callers that
                # don't pass the field get the original "all visible" behavior.
                selected_sids = (
                    set(credentials.selected_phone_sids)
                    if credentials.selected_phone_sids is not None
                    else None
                )

                # Store phone numbers. Single-owner invariant per provider_sid:
                # a Twilio number can only ever exist in ONE user's account
                # because Twilio itself stores a single voice_url per number —
                # if multiple users "claim" the same number, their assignments
                # silently overwrite each other. We reject claims by users
                # other than the existing owner; a force-transfer flow can be
                # added later if needed.
                rejected_sids: list[str] = []
                for record in incoming_phone_numbers:
                    existing_owner = phone_numbers_collection.find_one(
                        {"provider_sid": record.sid, "user_id": {"$ne": user_obj_id}},
                        {"user_id": 1},
                    )
                    if existing_owner:
                        rejected_sids.append(record.sid)
                        logger.warning(
                            "[IMPORT] Skipping %s (sid=%s) — already owned by "
                            "user %s; refusing cross-tenant duplicate",
                            record.phone_number, record.sid, existing_owner["user_id"],
                        )
                        continue

                    is_visible = selected_sids is None or record.sid in selected_sids
                    phone_doc = {
                        "user_id": user_obj_id,
                        "phone_number": record.phone_number,
                        "provider": "twilio",
                        "provider_sid": record.sid,
                        "friendly_name": record.friendly_name or record.phone_number,
                        "capabilities": {
                            "voice": record.capabilities.get("voice", False),
                            "sms": record.capabilities.get("sms", False),
                            "mms": record.capabilities.get("mms", False)
                        },
                        "status": "active",
                        "hidden": not is_visible,
                        "created_at": now,
                        "updated_at": now
                    }

                    # Upsert phone number — keyed on provider_sid alone now
                    # that single-owner is enforced above. Old code keyed on
                    # (user_id, provider_sid) which permitted the duplicate
                    # docs that caused cross-tenant routing bugs.
                    result = phone_numbers_collection.update_one(
                        {"provider_sid": record.sid},
                        {"$set": phone_doc},
                        upsert=True
                    )

                    # Get the document ID
                    if result.upserted_id:
                        doc_id = str(result.upserted_id)
                    else:
                        doc = phone_numbers_collection.find_one({"provider_sid": record.sid})
                        doc_id = str(doc["_id"])

                    # Critical: every Twilio number under our management
                    # must route to /api/twilio-webhooks/voice (the dynamic
                    # by-To-number router). Otherwise inbound calls to
                    # imported numbers go to whatever URL Twilio had
                    # previously — often a deprecated/dead endpoint, OR
                    # the customer's previous app — which silently breaks
                    # before the assistant gets dispatched. This is the
                    # default-on enforcement; future "Change AI" actions
                    # are no-ops for Twilio (URL doesn't change), they
                    # only update Mongo's assigned_assistant_id.
                    from app.utils.twilio_voice_routing import ensure_unified_voice_routing
                    ensure_unified_voice_routing(
                        client, record.sid, label=record.phone_number,
                    )

                    # Only return visible numbers in the response — caller
                    # expects this to match what they'll see on the dashboard.
                    if is_visible:
                        phone_numbers.append(PhoneNumberResponse(
                            id=doc_id,
                            phone_number=record.phone_number,
                            provider="twilio",
                            friendly_name=record.friendly_name or record.phone_number,
                            capabilities=PhoneNumberCapabilities(
                                voice=record.capabilities.get("voice", False),
                                sms=record.capabilities.get("sms", False),
                                mms=record.capabilities.get("mms", False)
                            ),
                            status="active",
                            hidden=False,
                            created_at=now.isoformat() + "Z"
                        ))

                logger.info(f"Successfully synced {len(phone_numbers)} phone numbers")

                return ConnectProviderResponse(
                    message=f"Successfully connected Twilio and synced {len(phone_numbers)} phone numbers",
                    phone_numbers=phone_numbers,
                    provider="twilio"
                )

            except TwilioRestException as e:
                logger.error(f"Twilio API error: {str(e)}")
                # Return proper status code based on Twilio error
                if e.status == 401 or e.code == 20003:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid Twilio credentials. Please check your Account SID and Auth Token."
                    )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Twilio API error: {str(e)}"
                )
            except Exception as e:
                error_str = str(e)
                logger.error(f"Error connecting to Twilio: {error_str}")
                # Check if it's an auth error in the message
                if "401" in error_str or "Authenticate" in error_str or "20003" in error_str:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid Twilio credentials. Please check your Account SID and Auth Token."
                    )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Error connecting to Twilio: {error_str}"
                )

        elif credentials.provider.lower() == "vobiz":
            # Vobiz has no public API to enumerate numbers — the user provides
            # the E.164 number plus the LiveKit outbound SIP trunk we should
            # dial through. We just create the phone_numbers doc directly.
            if not credentials.phone_number:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="phone_number is required for Vobiz (E.164 format, e.g. +918065481572)"
                )
            if not credentials.livekit_outbound_trunk_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="livekit_outbound_trunk_id is required for Vobiz (e.g. ST_oSruuDU6KtFJ)"
                )

            E164 = re.compile(r"^\+[1-9]\d{1,14}$")
            phone_e164 = credentials.phone_number.strip()
            if not E164.match(phone_e164):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid phone_number format. Use E.164 (e.g. +918065481572)."
                )

            now = datetime.utcnow()
            friendly = credentials.friendly_name or phone_e164
            phone_doc = {
                "user_id": user_obj_id,
                "phone_number": phone_e164,
                "provider": "vobiz",
                "livekit_outbound_trunk_id": credentials.livekit_outbound_trunk_id.strip(),
                "friendly_name": friendly,
                "capabilities": {"voice": True, "sms": False, "mms": False},
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }
            # Upsert by (user, phone_number) so re-submitting is idempotent
            result = phone_numbers_collection.update_one(
                {"user_id": user_obj_id, "phone_number": phone_e164},
                {"$set": phone_doc},
                upsert=True,
            )
            doc_id = str(result.upserted_id) if result.upserted_id else str(
                phone_numbers_collection.find_one({"user_id": user_obj_id, "phone_number": phone_e164})["_id"]
            )

            # Record provider connection (no credentials to store — IP/SIP-trunk based)
            provider_connections_collection.update_one(
                {"user_id": user_obj_id, "provider": "vobiz"},
                {"$set": {
                    "user_id": user_obj_id,
                    "provider": "vobiz",
                    "status": "active",
                    "updated_at": now,
                }, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )

            logger.info(f"Connected Vobiz number {phone_e164} (trunk {credentials.livekit_outbound_trunk_id}) for user {credentials.user_id}")

            return ConnectProviderResponse(
                message=f"Vobiz number {phone_e164} added",
                phone_numbers=[PhoneNumberResponse(
                    id=doc_id,
                    phone_number=phone_e164,
                    provider="vobiz",
                    friendly_name=friendly,
                    capabilities=PhoneNumberCapabilities(voice=True, sms=False, mms=False),
                    status="active",
                    created_at=now.isoformat() + "Z",
                )],
                provider="vobiz",
            )

        else:
            # Placeholder for other providers
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Provider '{credentials.provider}' is not yet supported. Currently supported: Twilio, Vobiz."
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in connect_provider: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {str(e)}"
        )


@router.delete("/disconnect/{user_id}", status_code=status.HTTP_200_OK)
async def disconnect_provider(
    user_id: str,
    provider: str = "twilio",
    current_user: dict = Depends(get_current_user),
):
    """
    Disconnect a provider and remove stored credentials.

    Auth: JWT required AND the URL user_id must equal the JWT subject. Without
    this, any logged-in user could disconnect any other user's provider —
    a denial-of-service vector with one HTTP DELETE.
    """
    try:
        db = Database.get_db()
        users_collection = db['users']
        provider_connections_collection = db['provider_connections']

        # Enforce JWT subject == URL user_id (no cross-tenant disconnect).
        if user_id != current_user.get("user_id"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot disconnect another user's provider",
            )

        logger.info(f"Disconnecting {provider} for user: {user_id}")

        # Validate user_id format
        try:
            user_obj_id = ObjectId(user_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user_id format"
            )

        user = users_collection.find_one({"_id": user_obj_id})
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Remove provider connection
        result = provider_connections_collection.delete_one({
            "user_id": user_obj_id,
            "provider": provider
        })

        if result.deleted_count == 0:
            return {"message": f"No {provider} connection found to disconnect", "disconnected": False}

        logger.info(f"Successfully disconnected {provider} for user {user_id}")
        return {"message": f"Successfully disconnected {provider}. You can now reconnect with fresh credentials.", "disconnected": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error disconnecting provider: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error disconnecting provider: {str(e)}"
        )


@router.post("/sync/{user_id}", response_model=ConnectProviderResponse, status_code=status.HTTP_200_OK)
async def sync_phone_numbers(user_id: str):
    """
    Sync phone numbers using existing provider connection
    (No need to re-enter credentials)

    Args:
        user_id: User ID

    Returns:
        ConnectProviderResponse: List of synced phone numbers

    Raises:
        HTTPException: If no provider connection found or sync fails
    """
    try:
        db = Database.get_db()
        users_collection = db['users']
        phone_numbers_collection = db['phone_numbers']
        provider_connections_collection = db['provider_connections']

        logger.info(f"Syncing phone numbers for user: {user_id}")

        # Verify user exists
        try:
            user_obj_id = ObjectId(user_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user_id format"
            )

        user = users_collection.find_one({"_id": user_obj_id})
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Get existing Twilio connection
        twilio_connection = provider_connections_collection.find_one({
            "user_id": user_obj_id,
            "provider": "twilio"
        })

        if not twilio_connection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No Twilio connection found. Please connect Twilio first."
            )

        phone_numbers = []

        try:
            # Initialize Twilio client with stored credentials
            account_sid, auth_token = decrypt_twilio_credentials(twilio_connection)
            if not account_sid or not auth_token:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Stored Twilio credentials are missing or invalid. Please reconnect your provider."
                )

            client = Client(account_sid, auth_token)

            # Fetch phone numbers
            incoming_phone_numbers = client.incoming_phone_numbers.list(limit=50)

            logger.info(f"Found {len(incoming_phone_numbers)} phone numbers from Twilio")

            # Store phone numbers. Single-owner invariant per provider_sid —
            # see the connect-provider route's matching block. Skip numbers
            # already owned by another user instead of creating duplicate docs.
            now = datetime.utcnow()
            for record in incoming_phone_numbers:
                existing_owner = phone_numbers_collection.find_one(
                    {"provider_sid": record.sid, "user_id": {"$ne": user_obj_id}},
                    {"user_id": 1},
                )
                if existing_owner:
                    logger.warning(
                        "[REFRESH] Skipping %s (sid=%s) — already owned by "
                        "user %s; refusing cross-tenant duplicate",
                        record.phone_number, record.sid, existing_owner["user_id"],
                    )
                    continue

                phone_doc = {
                    "user_id": user_obj_id,
                    "phone_number": record.phone_number,
                    "provider": "twilio",
                    "provider_sid": record.sid,
                    "friendly_name": record.friendly_name or record.phone_number,
                    "capabilities": {
                        "voice": record.capabilities.get("voice", False),
                        "sms": record.capabilities.get("sms", False),
                        "mms": record.capabilities.get("mms", False)
                    },
                    "status": "active",
                    "updated_at": now
                }

                # Upsert phone number (preserve existing assignments). Keyed
                # on provider_sid alone — single-owner enforced above.
                result = phone_numbers_collection.update_one(
                    {"provider_sid": record.sid},
                    {"$set": phone_doc, "$setOnInsert": {"created_at": now}},
                    upsert=True
                )

                # Re-ensure unified voice routing on every refresh — protects
                # against drift if someone manually changed Twilio's voice_url
                # between syncs, or if the number was previously configured
                # with a TwiML App / per-assistant URL.
                from app.utils.twilio_voice_routing import ensure_unified_voice_routing
                ensure_unified_voice_routing(
                    client, record.sid, label=record.phone_number,
                )

                # Get the document
                doc = phone_numbers_collection.find_one({"provider_sid": record.sid})

                # Skip hidden numbers in the response so the frontend doesn't
                # overwrite its filtered list with all-9 visible cards. The
                # number stays in MongoDB (Twilio is its source of truth) — we
                # just don't return it here.
                if doc.get("hidden") is True:
                    continue

                phone_numbers.append(PhoneNumberResponse(
                    id=str(doc["_id"]),
                    phone_number=record.phone_number,
                    provider="twilio",
                    friendly_name=record.friendly_name or record.phone_number,
                    capabilities=PhoneNumberCapabilities(
                        voice=record.capabilities.get("voice", False),
                        sms=record.capabilities.get("sms", False),
                        mms=record.capabilities.get("mms", False)
                    ),
                    status="active",
                    created_at=doc.get("created_at", now).isoformat() + "Z",
                    assigned_assistant_id=str(doc["assigned_assistant_id"]) if doc.get("assigned_assistant_id") else None,
                    assigned_assistant_name=doc.get("assigned_assistant_name"),
                    webhook_url=doc.get("webhook_url"),
                    hidden=False,  # only visible numbers reach this branch
                ))

            logger.info(f"Successfully synced {len(phone_numbers)} phone numbers")

            return ConnectProviderResponse(
                message=f"Successfully synced {len(phone_numbers)} phone numbers from Twilio",
                phone_numbers=phone_numbers,
                provider="twilio"
            )

        except CredentialDecryptionError as e:
            logger.error(f"Credential decryption failed: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(e)
            )
        except TwilioRestException as e:
            logger.error(f"Twilio API error during sync: {str(e)}")
            # Return proper status code based on Twilio error
            if e.status == 401 or e.code == 20003:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Stored Twilio credentials are invalid. Please reconnect your Twilio account."
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Twilio API error: {str(e)}"
            )
        except Exception as e:
            error_str = str(e)
            logger.error(f"Error syncing phone numbers: {error_str}")
            # Check if it's an auth error in the message
            if "401" in error_str or "Authenticate" in error_str or "20003" in error_str:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Stored Twilio credentials are invalid. Please reconnect your Twilio account."
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error syncing phone numbers: {error_str}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in sync_phone_numbers: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {str(e)}"
        )


@router.get("/user/{user_id}", response_model=PhoneNumberListResponse)
async def get_user_phone_numbers(user_id: str, include_hidden: bool = False):
    """
    Get all phone numbers for a user
    Cached for 10 seconds for blazing fast page loads

    Args:
        user_id: User ID
        include_hidden: When True, also returns numbers the user has hidden
                        from the dashboard. Default False — the dashboard's
                        "Show hidden" toggle flips this on.

    Returns:
        PhoneNumberListResponse: List of user's phone numbers
    """
    try:
        # Cache key includes include_hidden so toggling doesn't return stale data.
        from app.utils.cache import get_from_cache, set_to_cache, generate_cache_key
        cache_key = generate_cache_key("phone_numbers:user", user_id, str(include_hidden))
        cached_result = await get_from_cache(cache_key)
        if cached_result:
            logger.debug(f"Cache hit for phone numbers: {user_id} include_hidden={include_hidden}")
            return cached_result
        db = Database.get_db()
        phone_numbers_collection = db['phone_numbers']

        # Validate user_id
        try:
            user_obj_id = ObjectId(user_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user_id format"
            )

        # Filter: active + (hidden=false OR include_hidden=true).
        query: dict = {"user_id": user_obj_id, "status": "active"}
        if not include_hidden:
            # Match docs where hidden is missing OR explicitly false. Older docs
            # don't have the field at all, so they show by default.
            query["$or"] = [{"hidden": {"$exists": False}}, {"hidden": False}]
        phone_docs = list(phone_numbers_collection.find(query))

        phone_numbers = []
        for doc in phone_docs:
            phone_numbers.append(PhoneNumberResponse(
                id=str(doc["_id"]),
                phone_number=doc["phone_number"],
                provider=doc["provider"],
                friendly_name=doc.get("friendly_name"),
                capabilities=PhoneNumberCapabilities(**doc["capabilities"]),
                status=doc.get("status", "active"),
                created_at=doc["created_at"].isoformat() + "Z",
                assigned_assistant_id=str(doc["assigned_assistant_id"]) if doc.get("assigned_assistant_id") else None,
                assigned_assistant_name=doc.get("assigned_assistant_name"),
                webhook_url=doc.get("webhook_url"),
                hidden=bool(doc.get("hidden", False)),
            ))

        result = PhoneNumberListResponse(
            phone_numbers=phone_numbers,
            total=len(phone_numbers)
        )

        # Cache the result for 10 seconds (blazing fast page loads)
        await set_to_cache(cache_key, result.dict(), expire=10)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching phone numbers: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching phone numbers: {str(e)}"
        )


@router.get("/active-calls/{user_id}", status_code=status.HTTP_200_OK)
async def get_active_calls(user_id: str):
    """
    Get phone numbers with active calls for real-time indicators

    Args:
        user_id: User ID

    Returns:
        List of phone numbers with active calls
    """
    try:
        db = Database.get_db()
        call_logs_collection = db['call_logs']

        # Validate user_id
        try:
            user_obj_id = ObjectId(user_id)
        except Exception:
            return {"active_numbers": []}

        active_statuses = ["in-progress", "ringing", "queued", "initiated"]

        # Find calls in active status (support both legacy `call_status` and new `status` fields)
        active_calls = list(call_logs_collection.find({
            "user_id": user_obj_id,
            "$or": [
                {"call_status": {"$in": active_statuses}},
                {"status": {"$in": active_statuses}}
            ]
        }))

        # Extract unique phone numbers from both incoming and outgoing
        active_numbers = set()
        for call in active_calls:
            # Add the number being called (to_number for outbound, from user's perspective)
            if call.get("to_number"):
                active_numbers.add(call["to_number"])
            # For inbound calls, the user's number is the 'to' number
            # For outbound, it's the 'from_number'
            if call.get("from_number") and call.get("call_type") == "outbound":
                active_numbers.add(call["from_number"])

        logger.info(f"Found {len(active_numbers)} numbers with active calls for user {user_id}")
        return {"active_numbers": list(active_numbers)}

    except Exception as e:
        logger.error(f"Error fetching active calls: {e}")
        return {"active_numbers": []}


@router.get("/call-logs/user/{user_id}", response_model=CallLogListResponse)
async def get_user_call_logs(
    user_id: str,
    limit: int = 500,
    skip: int = 0,
    current_user: dict = Depends(get_current_user),
):
    """
    Get comprehensive call logs for the user. Caller must own the user_id
    (or be admin) — these are PII (caller numbers, transcripts).
    """
    await verify_user_ownership(current_user, user_id)

    # Check cache first (5 minute cache for instant loads)
    from app.utils.cache import get_from_cache, set_to_cache, generate_cache_key
    cache_key = generate_cache_key("call_logs:user", user_id, str(limit), str(skip))
    cached_result = await get_from_cache(cache_key)
    if cached_result:
        logger.info(f"⚡ Cache hit for call logs: {user_id} - instant response")
        return CallLogListResponse(**cached_result)

    # Enforce maximum limit to prevent excessive loading
    limit = min(limit, 2000)
    try:
        db = Database.get_db()
        phone_numbers_collection = db['phone_numbers']
        provider_connections_collection = db['provider_connections']
        assistants_collection = db['assistants']

        # Validate user_id
        try:
            user_obj_id = ObjectId(user_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user_id format"
            )

        # Get user's phone numbers with assistant assignments (optional)
        phone_docs = list(phone_numbers_collection.find({"user_id": user_obj_id}))

        phone_to_assistant: dict[str, dict[str, str]] = {}
        user_phone_numbers: list[str] = []

        if phone_docs:
            for phone_doc in phone_docs:
                user_phone_numbers.append(phone_doc["phone_number"])
                if phone_doc.get("assigned_assistant_id"):
                    phone_to_assistant[phone_doc["phone_number"]] = {
                        "id": str(phone_doc["assigned_assistant_id"]),
                        "name": phone_doc.get("assigned_assistant_name", "Unknown Assistant")
                    }

        # Always include call logs stored in our database (covers outbound tracking)
        call_logs: List[CallLogResponse] = []
        processed_sids: Set[str] = set()
        call_logs_collection = db['call_logs']

        # PERFORMANCE: Use skip for pagination
        db_calls = list(
            call_logs_collection.find({"user_id": user_obj_id})
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )

        for db_call in db_calls:
            call_sid = db_call.get("call_sid")
            if call_sid:
                processed_sids.add(call_sid)

            assistant_info = None
            if db_call.get("assigned_assistant_id"):
                assistant_info = {
                    "id": str(db_call["assigned_assistant_id"]),
                    "name": db_call.get("assistant_name", "Unknown Assistant")
                }
            elif db_call.get("assistant_id"):
                assistant_info = {
                    "id": str(db_call["assistant_id"]),
                    "name": db_call.get("assistant_name", "Unknown Assistant")
                }

            # Get voice configuration from call log
            voice_config = db_call.get("voice_config", {})

            # Extract customer data from transcript (regex-based) and merge with DB-stored data (GPT-extracted)
            transcript_text = db_call.get("transcript") or db_call.get("transcription_text") or ""
            regex_customer_data = extract_customer_data(transcript_text) if transcript_text else {}
            db_customer_data = db_call.get("customer_data") or {}
            # Merge: DB data (GPT-extracted) takes priority, fallback to regex extraction
            customer_data = {**regex_customer_data, **db_customer_data} if (regex_customer_data or db_customer_data) else None

            # Transform recording URL to use proxy endpoint if it's a direct Twilio URL
            recording_url = db_call.get("recording_url")
            if recording_url and "api.twilio.com" in recording_url:
                # Extract recording SID from Twilio URL
                # URL formats:
                # - https://api.twilio.com/...../Recordings/RE*****.mp3
                # - https://account:token@api.twilio.com/.../Recordings/RE*****.mp3
                # Recording SID format: RE followed by 32 hex characters
                recording_sid_match = re.search(r'/Recordings/(RE[a-f0-9]{32})', recording_url, re.IGNORECASE)
                if recording_sid_match:
                    recording_sid = recording_sid_match.group(1)
                    recording_url = f"/api/phone-numbers/recording/{recording_sid}?user_id={user_id}"
                else:
                    # If we can't extract a valid SID, keep the original URL but log a warning
                    logger.warning(f"Could not extract recording SID from URL: {recording_url}")
                    recording_url = None

            call_log = CallLogResponse(
                id=call_sid or str(db_call["_id"]),
                **{"from": db_call.get("from_number") or "Unknown"},
                to=db_call.get("to_number") or "Unknown",
                direction=db_call.get("direction", "outbound-api"),
                status=db_call.get("status", "unknown"),
                duration=db_call.get("duration"),
                start_time=_serialize_datetime(db_call.get("start_time")),
                end_time=_serialize_datetime(db_call.get("end_time")),
                date_created=_serialize_datetime(
                    db_call.get("created_at") or db_call.get("started_at"),
                    fallback=True
                ),
                date_updated=_serialize_datetime(db_call.get("updated_at")),
                answered_by=None,
                caller_name=None,
                forwarded_from=None,
                parent_call_sid=None,
                price=None,
                price_unit=None,
                recording_url=recording_url,
                transcription_text=db_call.get("transcription_text") or db_call.get("transcript"),
                transcript=db_call.get("transcript"),
                summary=db_call.get("summary"),
                sentiment=db_call.get("sentiment"),
                sentiment_score=db_call.get("sentiment_score"),
                assistant_id=assistant_info["id"] if assistant_info else None,
                assistant_name=assistant_info["name"] if assistant_info else None,
                queue_time=None,
                asr_provider=voice_config.get("asr_provider"),
                asr_model=voice_config.get("asr_model"),
                tts_provider=voice_config.get("tts_provider"),
                tts_model=voice_config.get("tts_model"),
                llm_provider=voice_config.get("llm_provider"),
                llm_model=voice_config.get("llm_model"),
                customer_data=customer_data,
                # Include pre-calculated cost data from database
                cost_total=db_call.get("cost_total"),
                cost_api=db_call.get("cost_api"),
                cost_twilio=db_call.get("cost_twilio"),
                cost_currency=db_call.get("cost_currency"),
                cost_calculated=db_call.get("cost_calculated"),
                is_realtime_api=db_call.get("is_realtime_api"),
                # Include parsed conversation log from GPT analysis
                conversation_log=db_call.get("conversation_log")
            )
            call_logs.append(call_log)

        # Optionally augment with Twilio data if connection and credentials exist
        twilio_connection = provider_connections_collection.find_one({
            "user_id": user_obj_id,
            "provider": "twilio"
        })

        if twilio_connection:
            try:
                account_sid, auth_token = decrypt_twilio_credentials(twilio_connection)
                if not account_sid or not auth_token:
                    raise RuntimeError("Stored Twilio credentials are missing or invalid")

                client = Client(account_sid, auth_token)

                # PERFORMANCE FIX: Skip Twilio API entirely for instant loads
                # All calls are already stored in DB via webhooks, no need to fetch from Twilio
                # This makes call logs load instantly (< 100ms) instead of 5-10 seconds
                should_fetch_twilio = False  # Disabled for instant performance

                calls = []
                recordings_cache = {}
                transcriptions_cache = {}

                if should_fetch_twilio:
                    # PERFORMANCE FIX: Limit Twilio calls to prevent slow loading
                    # Use reasonable limit for Twilio API to prevent hanging
                    twilio_limit = min(limit - len(db_calls), 200)

                    # Fetch Twilio calls with error handling to prevent hanging
                    # Twilio client has built-in timeout, but we limit results to prevent slow responses
                    try:
                        calls = client.calls.list(limit=twilio_limit)
                        logger.debug(f"Fetched {len(calls)} calls from Twilio for user {user_id}")
                    except Exception as twilio_err:
                        logger.warning(f"Error fetching calls from Twilio for user {user_id}: {str(twilio_err)}")
                        # Continue with DB calls only if Twilio fails
                        calls = []

                    # PERFORMANCE OPTIMIZATION: Batch fetch all recordings and transcriptions once
                    # instead of making sequential API calls inside the loop
                    # This reduces API calls from N calls to 2 calls (90% faster)
                    if calls:  # Only fetch if we have calls
                        try:
                            # Fetch all recordings for recent calls with error handling
                            all_recordings = client.recordings.list(limit=200)
                            recordings_cache = {rec.call_sid: rec for rec in all_recordings}
                            logger.debug(f"Batch cached {len(recordings_cache)} recordings")
                        except Exception as rec_err:
                            logger.warning(f"Error fetching recordings: {str(rec_err)}")
                            recordings_cache = {}

                        try:
                            # Fetch all transcriptions for cached recordings
                            all_transcriptions = client.transcriptions.list(limit=200)
                            transcriptions_cache = {trans.recording_sid: trans for trans in all_transcriptions}
                            logger.debug(f"Batch cached {len(transcriptions_cache)} transcriptions")
                        except Exception as trans_err:
                            logger.warning(f"Error fetching transcriptions: {str(trans_err)}")
                            transcriptions_cache = {}
                else:
                    logger.debug(f"Skipping Twilio API fetch - already have {len(db_calls)} records from DB (skip={skip})")

                for call in calls:
                    if call.sid in processed_sids:
                        continue

                    from_number = getattr(call, 'from_', None) or getattr(call, 'from', None)

                    involves_user = False
                    if user_phone_numbers:
                        involves_user = (
                            call.to in user_phone_numbers or
                            from_number in user_phone_numbers
                        )
                    elif call.to or from_number:
                        # No stored numbers, include everything linked to the user account
                        involves_user = True

                    if not involves_user:
                        continue

                    assistant_info = None
                    if call.direction in ['inbound', 'trunking'] and call.to:
                        assistant_info = phone_to_assistant.get(call.to)

                    # Fetch recordings and transcriptions from Twilio (using batch cache)
                    recording_url = None
                    transcription_text = None

                    try:
                        # Look up recording from batch cache (performance optimization)
                        recording = recordings_cache.get(call.sid)
                        if recording:
                            # Validate recording SID format before using it
                            if recording.sid and len(recording.sid) == 34 and recording.sid.startswith('RE'):
                                # Use our proxy endpoint instead of direct Twilio URL
                                # This prevents browser authentication prompts
                                recording_url = f"/api/phone-numbers/recording/{recording.sid}?user_id={user_id}"
                                logger.debug(f"Found recording {recording.sid} for call {call.sid}")
                                
                                # Look up transcription from batch cache
                                transcription = transcriptions_cache.get(recording.sid)
                                if transcription:
                                    # Get transcription text, handle if it's empty
                                    if hasattr(transcription, 'transcription_text') and transcription.transcription_text:
                                        transcription_text = transcription.transcription_text
                                        logger.debug(f"Found transcription for recording {recording.sid}: {len(transcription_text)} chars")
                                    else:
                                        logger.debug(f"Transcription exists but text is empty for recording {recording.sid}")
                            else:
                                logger.warning(f"Invalid recording SID format: {recording.sid} for call {call.sid}")
                    except Exception as rec_err:
                        logger.debug(f"Error processing recording for call {call.sid}: {str(rec_err)}")

                    # Try to get cost data from database if it was calculated
                    db_call_data = call_logs_collection.find_one({"call_sid": call.sid})

                    # Extract customer data from transcription (regex-based) and merge with DB-stored data (GPT-extracted)
                    regex_customer_data = extract_customer_data(transcription_text) if transcription_text else {}
                    db_customer_data = db_call_data.get("customer_data") if db_call_data else {}
                    # Merge: DB data (GPT-extracted) takes priority, fallback to regex extraction
                    customer_data_twilio = {**regex_customer_data, **db_customer_data} if (regex_customer_data or db_customer_data) else None

                    call_log = CallLogResponse(
                        id=call.sid,
                        **{"from": getattr(call, 'from_formatted', None) or from_number or "Unknown"},
                        to=getattr(call, 'to_formatted', None) or call.to or "Unknown",
                        direction=call.direction,
                        status=call.status,
                        duration=int(call.duration) if call.duration else None,
                        start_time=_serialize_datetime(call.start_time),
                        end_time=_serialize_datetime(call.end_time),
                        date_created=_serialize_datetime(call.date_created, fallback=True),
                        date_updated=_serialize_datetime(call.date_updated),
                        answered_by=getattr(call, 'answered_by', None),
                        caller_name=getattr(call, 'caller_name', None),
                        forwarded_from=getattr(call, 'forwarded_from', None),
                        parent_call_sid=getattr(call, 'parent_call_sid', None),
                        price=call.price if call.price else None,
                        price_unit=getattr(call, 'price_unit', None) if call.price else None,
                        recording_url=recording_url,
                        transcription_text=transcription_text,
                        assistant_id=assistant_info["id"] if assistant_info else None,
                        assistant_name=assistant_info["name"] if assistant_info else None,
                        queue_time=getattr(call, 'queue_time', None),
                        asr_provider=None,
                        asr_model=None,
                        tts_provider=None,
                        tts_model=None,
                        llm_provider=None,
                        llm_model=None,
                        customer_data=customer_data_twilio,
                        # Include pre-calculated cost data from database if available
                        cost_total=db_call_data.get("cost_total") if db_call_data else None,
                        cost_api=db_call_data.get("cost_api") if db_call_data else None,
                        cost_twilio=db_call_data.get("cost_twilio") if db_call_data else None,
                        cost_currency=db_call_data.get("cost_currency") if db_call_data else None,
                        cost_calculated=db_call_data.get("cost_calculated") if db_call_data else None,
                        is_realtime_api=db_call_data.get("is_realtime_api") if db_call_data else None,
                        # Include parsed conversation log from GPT analysis
                        conversation_log=db_call_data.get("conversation_log") if db_call_data else None
                    )

                    call_logs.append(call_log)
            except TwilioRestException as e:
                logger.warning(f"Twilio API error fetching call logs for user {user_id}: {str(e)}")
            except Exception as e:
                logger.error(f"Error fetching call logs from Twilio for user {user_id}: {str(e)}")
                import traceback
                logger.error(traceback.format_exc())
        else:
            logger.info(f"No Twilio connection found for user {user_id}; returning database call logs only.")

        logger.info(f"Returning {len(call_logs)} call logs for user {user_id}")

        result = CallLogListResponse(
            call_logs=call_logs,
            total=len(call_logs)
        )

        # Cache the result for 5 minutes (instant loads, background refresh)
        await set_to_cache(cache_key, result.dict(), expire=300)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_user_call_logs: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred: {str(e)}"
        )


@router.post("/assign-assistant", response_model=AssignAssistantResponse, status_code=status.HTTP_200_OK)
async def assign_assistant_to_phone_number(
    request: Request,
    assignment: AssignAssistantRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Assign an AI assistant to a phone number and configure Twilio webhook.

    Auth invariant: the JWT subject must own BOTH the phone number AND the
    assistant. Without this check, user A could assign user B's number to
    their own assistant (or vice-versa) by guessing ObjectIds.

    Side effect: if the assistant already has another number assigned, that
    other number is automatically unassigned. Per-assistant single-number
    invariant keeps outbound + inbound routing deterministic — otherwise a
    Mongo find_one() picks one of the two non-deterministically.
    """
    try:
        db = Database.get_db()
        phone_numbers_collection = db['phone_numbers']
        assistants_collection = db['assistants']
        provider_connections_collection = db['provider_connections']

        try:
            jwt_user_obj_id = ObjectId(current_user["user_id"])
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid user")

        logger.info(f"Assigning assistant {assignment.assistant_id} to phone number {assignment.phone_number_id}")

        # Validate phone_number_id
        try:
            phone_obj_id = ObjectId(assignment.phone_number_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid phone_number_id format"
            )

        # Validate assistant_id
        try:
            assistant_obj_id = ObjectId(assignment.assistant_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid assistant_id format"
            )

        # Fetch phone number — caller must own it (404 not 403 to avoid leaking
        # existence of other users' numbers via probe).
        phone_doc = phone_numbers_collection.find_one({"_id": phone_obj_id, "user_id": jwt_user_obj_id})
        if not phone_doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Phone number not found"
            )

        # Fetch assistant — caller must own it (same probe-prevention).
        assistant_doc = assistants_collection.find_one({"_id": assistant_obj_id, "user_id": jwt_user_obj_id})
        if not assistant_doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="AI assistant not found"
            )

        # Generate webhook URL based on voice_mode
        # Use API_BASE_URL from settings if available (for production), otherwise use request URL
        if settings.api_base_url:
            base_url = settings.api_base_url
        else:
            base_url = f"{request.url.scheme}://{request.url.netloc}"

        # Route to custom stream handler
        webhook_url = f"{base_url}/api/inbound-calls/connect/{assignment.assistant_id}"
        logger.info(f"[PHONE] Webhook URL: {webhook_url}")

        # Auto-unassign any OTHER numbers currently pointing at this assistant.
        # One number per assistant — keeps outbound routing deterministic
        # (find_one() over multiple assignments was non-deterministic and
        # caused "I picked Twilio but call went via Vobiz" earlier today).
        other_assignments = phone_numbers_collection.update_many(
            {"assigned_assistant_id": assistant_obj_id, "_id": {"$ne": phone_obj_id}},
            {"$set": {
                "assigned_assistant_id": None,
                "assigned_assistant_name": None,
                "updated_at": datetime.utcnow(),
            }},
        )
        if other_assignments.modified_count:
            logger.info(
                "[ASSIGN] Unassigned %d previous number(s) from assistant %s",
                other_assignments.modified_count, assignment.assistant_id,
            )

        # Update phone number document
        update_doc = {
            "assigned_assistant_id": assistant_obj_id,
            "assigned_assistant_name": assistant_doc["name"],
            "webhook_url": webhook_url,
            "updated_at": datetime.utcnow()
        }

        phone_numbers_collection.update_one(
            {"_id": phone_obj_id},
            {"$set": update_doc}
        )

        webhook_configured = False

        # Re-assert unified voice routing on Twilio so INBOUND to this number
        # always reaches /api/twilio-webhooks/voice — which then routes by
        # To-number to whichever assistant is assigned here. Idempotent, so it's
        # safe to run on every assign (self-heals a stale voice_url). Account:
        # a BYO connection if the user has one, otherwise the shared platform
        # Twilio account (env creds) — which backs every number bought via the
        # in-app "Purchase Number" flow.
        if phone_doc["provider"].lower() == "twilio" and phone_doc.get("provider_sid"):
            try:
                twilio_connection = provider_connections_collection.find_one({
                    "user_id": phone_doc["user_id"],
                    "provider": "twilio"
                })
                cfg_sid = cfg_token = None
                if twilio_connection:
                    try:
                        cfg_sid, cfg_token = decrypt_twilio_credentials(twilio_connection)
                    except Exception:
                        cfg_sid = cfg_token = None
                if not (cfg_sid and cfg_token):
                    from app.config.settings import settings as _settings
                    cfg_sid = _settings.twilio_account_sid
                    cfg_token = _settings.twilio_auth_token
                if cfg_sid and cfg_token:
                    # ensure_unified_voice_routing also clears voice_application_sid
                    # (a TwiML App would otherwise silently override voice_url).
                    from app.utils.twilio_voice_routing import ensure_unified_voice_routing
                    client = Client(cfg_sid, cfg_token)
                    webhook_configured, _ = ensure_unified_voice_routing(
                        client, phone_doc["provider_sid"],
                        label=phone_doc["phone_number"],
                    )
                else:
                    logger.warning("[ASSIGN] no Twilio credentials for %s — voice webhook not (re)configured",
                                   phone_doc.get("phone_number"))
            except Exception as e:
                logger.error(f"[ASSIGN] error (re)configuring Twilio voice webhook for {phone_doc.get('phone_number')}: {e}")
                # Don't fail the assignment if webhook config fails — Mongo already
                # records it, and a freshly-purchased number already has
                # voice_url=/voice from purchase time.
                webhook_configured = False

        # Fetch updated phone number
        updated_phone = phone_numbers_collection.find_one({"_id": phone_obj_id})

        phone_response = PhoneNumberResponse(
            id=str(updated_phone["_id"]),
            phone_number=updated_phone["phone_number"],
            provider=updated_phone["provider"],
            friendly_name=updated_phone.get("friendly_name"),
            capabilities=PhoneNumberCapabilities(**updated_phone["capabilities"]),
            status=updated_phone.get("status", "active"),
            created_at=updated_phone["created_at"].isoformat() + "Z",
            assigned_assistant_id=str(updated_phone["assigned_assistant_id"]),
            assigned_assistant_name=updated_phone["assigned_assistant_name"],
            webhook_url=updated_phone["webhook_url"]
        )

        return AssignAssistantResponse(
            message="AI assistant assigned successfully" + (" and webhook configured" if webhook_configured else ""),
            phone_number=phone_response,
            webhook_configured=webhook_configured
        )

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"Error assigning assistant to phone number: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error assigning assistant: {str(e)}"
        )


@router.delete("/unassign-assistant/{phone_number_id}", response_model=PhoneNumberResponse, status_code=status.HTTP_200_OK)
async def unassign_assistant_from_phone_number(
    phone_number_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Remove AI assistant assignment from a phone number.

    Auth: JWT required AND the caller must own the phone number. Returns 404
    if not owned (probe-prevention).
    """
    try:
        db = Database.get_db()
        phone_numbers_collection = db['phone_numbers']
        provider_connections_collection = db['provider_connections']

        try:
            jwt_user_obj_id = ObjectId(current_user["user_id"])
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid user")

        logger.info(f"Unassigning assistant from phone number {phone_number_id}")

        # Validate phone_number_id
        try:
            phone_obj_id = ObjectId(phone_number_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid phone_number_id format"
            )

        # Caller must own the number; 404 keeps the existence opaque to
        # cross-tenant probes.
        phone_doc = phone_numbers_collection.find_one({"_id": phone_obj_id, "user_id": jwt_user_obj_id})
        if not phone_doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Phone number not found"
            )

        # Update phone number document - remove assignment
        update_doc = {
            "updated_at": datetime.utcnow()
        }

        phone_numbers_collection.update_one(
            {"_id": phone_obj_id},
            {
                "$set": update_doc,
                "$unset": {
                    "assigned_assistant_id": "",
                    "assigned_assistant_name": "",
                    "webhook_url": ""
                }
            }
        )

        # Remove Twilio webhook if it's a Twilio number
        if phone_doc["provider"].lower() == "twilio":
            try:
                twilio_connection = provider_connections_collection.find_one({
                    "user_id": phone_doc["user_id"],
                    "provider": "twilio"
                })

                if twilio_connection:
                    account_sid, auth_token = decrypt_twilio_credentials(twilio_connection)
                    if account_sid and auth_token:
                        client = Client(account_sid, auth_token)
                    else:
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Stored Twilio credentials are missing or invalid. Please reconnect your provider."
                        )

                    # Clear the voice webhook
                    incoming_phone_number = client.incoming_phone_numbers(phone_doc["provider_sid"]).update(
                        voice_url='',
                        voice_method='POST'
                    )

                    logger.info(f"Successfully removed Twilio webhook for {phone_doc['phone_number']}")

            except Exception as e:
                logger.error(f"Error removing Twilio webhook: {str(e)}")

        # Fetch updated phone number
        updated_phone = phone_numbers_collection.find_one({"_id": phone_obj_id})

        phone_response = PhoneNumberResponse(
            id=str(updated_phone["_id"]),
            phone_number=updated_phone["phone_number"],
            provider=updated_phone["provider"],
            friendly_name=updated_phone.get("friendly_name"),
            capabilities=PhoneNumberCapabilities(**updated_phone["capabilities"]),
            status=updated_phone.get("status", "active"),
            created_at=updated_phone["created_at"].isoformat() + "Z",
            assigned_assistant_id=None,
            assigned_assistant_name=None,
            webhook_url=None
        )

        return phone_response

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        logger.error(f"Error unassigning assistant from phone number: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error unassigning assistant: {str(e)}"
        )


@router.patch("/{phone_number_id}/visibility", response_model=PhoneNumberResponse, status_code=status.HTTP_200_OK)
async def set_phone_number_visibility(
    phone_number_id: str,
    hidden: bool,
    current_user: dict = Depends(get_current_user),
):
    """Toggle whether a phone number is shown on the dashboard's default list.

    Hidden numbers are filtered out of GET /user/{user_id} unless that endpoint
    is called with `include_hidden=true`. Useful when an account has many
    auto-purchased numbers and the user only wants to see the ones in active use.

    Auth: JWT required AND the caller must own the number. Returns 404 if not
    owned (probe-prevention — same pattern as unassign-assistant above).
    """
    db = Database.get_db()
    phone_numbers_collection = db['phone_numbers']

    try:
        phone_obj_id = ObjectId(phone_number_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid phone_number_id format",
        )

    user_obj_id = ObjectId(current_user["id"]) if isinstance(current_user.get("id"), str) else current_user.get("id")
    phone_doc = phone_numbers_collection.find_one({"_id": phone_obj_id, "user_id": user_obj_id})
    if not phone_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Phone number not found")

    phone_numbers_collection.update_one(
        {"_id": phone_obj_id},
        {"$set": {"hidden": bool(hidden), "updated_at": datetime.utcnow()}},
    )

    # Invalidate cached list so the toggle takes effect immediately. Both
    # cache keys (with/without hidden) need to be cleared.
    try:
        from app.utils.cache import delete_from_cache, generate_cache_key
        await delete_from_cache(generate_cache_key("phone_numbers:user", str(user_obj_id), "False"))
        await delete_from_cache(generate_cache_key("phone_numbers:user", str(user_obj_id), "True"))
    except Exception:
        logger.exception("Failed to invalidate phone-numbers cache after visibility toggle")

    updated = phone_numbers_collection.find_one({"_id": phone_obj_id})
    return PhoneNumberResponse(
        id=str(updated["_id"]),
        phone_number=updated["phone_number"],
        provider=updated["provider"],
        friendly_name=updated.get("friendly_name"),
        capabilities=PhoneNumberCapabilities(**updated["capabilities"]),
        status=updated.get("status", "active"),
        created_at=updated["created_at"].isoformat() + "Z",
        assigned_assistant_id=str(updated["assigned_assistant_id"]) if updated.get("assigned_assistant_id") else None,
        assigned_assistant_name=updated.get("assigned_assistant_name"),
        webhook_url=updated.get("webhook_url"),
        hidden=bool(updated.get("hidden", False)),
    )



@router.get("/recording/{recording_sid}")
async def get_call_recording(recording_sid: str, user_id: str):
    """
    Proxy endpoint to fetch call recordings from Twilio without exposing credentials

    Args:
        recording_sid: Twilio recording SID
        user_id: User ID to verify ownership

    Returns:
        StreamingResponse: Audio file stream
    """
    from fastapi.responses import StreamingResponse
    import requests

    try:
        logger.info(f"Fetching recording {recording_sid} for user {user_id}")

        db = Database.get_db()
        provider_connections_collection = db['provider_connections']

        # Validate user_id
        try:
            user_obj_id = ObjectId(user_id)
        except Exception as e:
            logger.error(f"Invalid user_id format: {user_id}, error: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user_id format"
            )

        # Get user's Twilio credentials
        twilio_connection = provider_connections_collection.find_one({
            "user_id": user_obj_id,
            "provider": "twilio"
        })

        if not twilio_connection:
            logger.error(f"Twilio connection not found for user {user_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Twilio connection not found"
            )

        account_sid, auth_token = decrypt_twilio_credentials(twilio_connection)
        if not account_sid or not auth_token:
            logger.error(f"Invalid Twilio credentials for user {user_id}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid Twilio credentials"
            )

        # Construct Twilio recording URL
        recording_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording_sid}.mp3"
        logger.info(f"Fetching recording from Twilio: {recording_url}")

        # Fetch recording from Twilio with authentication and timeout
        response = requests.get(
            recording_url,
            auth=(account_sid, auth_token),
            stream=True,
            timeout=60  # 60 second timeout for large recordings
        )

        if response.status_code != 200:
            logger.error(f"Recording not found at Twilio. Status: {response.status_code}, Recording SID: {recording_sid}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recording not found (Twilio returned {response.status_code})"
            )

        # Stream the recording to the client
        return StreamingResponse(
            response.iter_content(chunk_size=8192),
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": f"inline; filename=recording-{recording_sid}.mp3",
                "Accept-Ranges": "bytes"
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching recording: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching recording: {str(e)}"
        )


@router.post("/transcribe-recordings/{user_id}")
async def transcribe_all_recordings(user_id: str):
    """
    Manually trigger transcription for all recordings that don't have transcripts yet.
    Uses OpenAI Whisper API to transcribe recordings.

    Args:
        user_id: User ID to transcribe recordings for

    Returns:
        Summary of transcription results
    """
    import asyncio
    from app.services.async_inbound_post_call_processor import AsyncInboundPostCallProcessor

    try:
        logger.info(f"Starting batch transcription for user {user_id}")

        db = Database.get_db()
        provider_connections_collection = db['provider_connections']
        call_logs_collection = db['call_logs']

        # Validate user_id
        try:
            user_obj_id = ObjectId(user_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user_id format"
            )

        # Get user's Twilio credentials
        twilio_connection = provider_connections_collection.find_one({
            "user_id": user_obj_id,
            "provider": "twilio"
        })

        if not twilio_connection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Twilio connection not found"
            )

        account_sid, auth_token = decrypt_twilio_credentials(twilio_connection)
        if not account_sid or not auth_token:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Invalid Twilio credentials"
            )

        # Initialize Twilio client and processor (OPTIMIZED: using async processor)
        client = Client(account_sid, auth_token)
        processor = AsyncInboundPostCallProcessor()

        # Get all user's call logs without transcriptions
        call_logs_without_transcription = list(call_logs_collection.find({
            "user_id": user_obj_id,
            "$or": [
                {"transcription_text": {"$exists": False}},
                {"transcription_text": None},
                {"transcription_text": ""}
            ]
        }).limit(50))  # Limit to 50 to avoid timeout

        logger.info(f"Found {len(call_logs_without_transcription)} calls without transcriptions")

        transcribed_count = 0
        failed_count = 0
        skipped_count = 0

        for call_log in call_logs_without_transcription:
            call_sid = call_log.get("call_sid")
            if not call_sid:
                skipped_count += 1
                continue

            try:
                # Fetch recordings for this call from Twilio
                recordings = client.recordings.list(call_sid=call_sid, limit=1)

                if not recordings:
                    logger.debug(f"No recording found for call {call_sid}")
                    skipped_count += 1
                    continue

                recording = recordings[0]
                recording_url = f"https://api.twilio.com{recording.uri.replace('.json', '.mp3')}"

                # Download recording
                audio_bytes = await processor.download_recording(recording_url)
                if not audio_bytes:
                    logger.error(f"Failed to download recording for call {call_sid}")
                    failed_count += 1
                    continue

                # Transcribe audio
                transcript = await processor.transcribe_audio(audio_bytes)
                if not transcript:
                    logger.error(f"Failed to transcribe recording for call {call_sid}")
                    failed_count += 1
                    continue

                # Save transcription to database
                call_logs_collection.update_one(
                    {"call_sid": call_sid},
                    {"$set": {
                        "transcription_text": transcript,
                        "transcription_status": "completed",
                        "updated_at": datetime.utcnow()
                    }}
                )

                logger.info(f"Transcribed call {call_sid}: {len(transcript)} characters")
                transcribed_count += 1

            except Exception as e:
                logger.error(f"Error transcribing call {call_sid}: {str(e)}")
                failed_count += 1

        return {
            "status": "success",
            "transcribed": transcribed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "total_processed": len(call_logs_without_transcription)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in batch transcription: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error transcribing recordings: {str(e)}"
        )


@router.post("/recalculate-costs/{user_id}")
async def recalculate_user_call_costs(user_id: str, limit: int = 100):
    """
    Recalculate costs for all calls that don't have cost data yet

    Args:
        user_id: User ID to recalculate costs for
        limit: Maximum number of calls to process (default: 100)

    Returns:
        Summary of recalculation results
    """
    try:
        from app.services.cost_calculator import recalculate_all_call_costs

        logger.info(f"Starting cost recalculation for user {user_id}, limit={limit}")
        result = await recalculate_all_call_costs(user_id=user_id, limit=limit)

        return {
            "message": "Cost recalculation completed",
            "user_id": user_id,
            "results": result
        }

    except Exception as e:
        logger.error(f"Error in cost recalculation: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error recalculating costs: {str(e)}"
        )


@router.get("/call-cost/{call_sid}")
async def calculate_call_cost(call_sid: str, currency: str = "USD"):
    """
    Calculate the cost for a specific call based on its configuration and duration

    Args:
        call_sid: Call SID to calculate cost for
        currency: Currency to display cost in ("USD" or "INR", default: "USD")

    Returns:
        Dict with cost breakdown including API cost, Twilio cost, and total
    """
    try:
        db = Database.get_db()
        call_logs_collection = db['call_logs']

        # Get call log from database
        call_log = call_logs_collection.find_one({"call_sid": call_sid})
        if not call_log:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Call log not found for call_sid: {call_sid}"
            )

        # Get call duration in minutes
        duration_seconds = call_log.get("duration", 0)
        if duration_seconds is None:
            duration_seconds = 0
        duration_minutes = duration_seconds / 60.0

        # Initialize pricing calculator
        calculator = PricingCalculator(currency=currency.upper())

        # Check if call used custom providers or OpenAI Realtime
        is_custom_mode = (
            call_log.get("asr_provider") or
            call_log.get("llm_provider") or
            call_log.get("tts_provider")
        )

        if is_custom_mode:
            # Custom provider pipeline cost
            cost_breakdown = calculator.calculate_custom_pipeline_cost(
                asr_provider=call_log.get("asr_provider", "deepgram"),
                asr_model=call_log.get("asr_model", "nova-2"),
                llm_provider=call_log.get("llm_provider", "openai"),
                llm_model=call_log.get("llm_model", "gpt-4-turbo"),
                tts_provider=call_log.get("tts_provider", "elevenlabs"),
                tts_model=call_log.get("tts_model", "eleven_flash_v2_5"),
                duration_minutes=duration_minutes,
                estimated_tokens_in=call_log.get("estimated_tokens_in", 500),
                estimated_tokens_out=call_log.get("estimated_tokens_out", 300),
                estimated_tts_chars=call_log.get("estimated_tts_chars", 1000)
            )
        else:
            # OpenAI Realtime API cost
            voice_config = call_log.get("voice_config", {})
            model = voice_config.get("model", "gpt-4o-realtime")

            cost_breakdown = calculator.calculate_realtime_api_cost(
                model=model,
                duration_minutes=duration_minutes
            )

        # Add call metadata
        cost_breakdown["call_sid"] = call_sid
        cost_breakdown["call_direction"] = call_log.get("direction", "unknown")
        cost_breakdown["call_status"] = call_log.get("status", "unknown")

        return cost_breakdown

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error calculating call cost: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error calculating call cost: {str(e)}"
        )


@router.get("/estimate-cost")
async def estimate_call_cost(
    voice_mode: str = "custom",
    duration_minutes: float = 1.0,
    currency: str = "USD",
    model: Optional[str] = None,
    asr_provider: Optional[str] = None,
    asr_model: Optional[str] = None,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    tts_provider: Optional[str] = None,
    tts_model: Optional[str] = None
):
    """
    Estimate the cost for a call based on configuration

    Args:
        voice_mode: "realtime" or "custom"
        duration_minutes: Call duration in minutes (default: 1.0)
        currency: Currency to display cost in ("USD" or "INR", default: "USD")
        model: OpenAI Realtime model (for realtime mode)
        asr_provider: ASR provider (for custom mode)
        asr_model: ASR model (for custom mode)
        llm_provider: LLM provider (for custom mode)
        llm_model: LLM model (for custom mode)
        tts_provider: TTS provider (for custom mode)
        tts_model: TTS model (for custom mode)

    Returns:
        Dict with estimated cost breakdown
    """
    try:
        calculator = PricingCalculator(currency=currency.upper())

        is_realtime = voice_mode.lower() == "realtime"

        cost_estimate = calculator.get_per_minute_estimate(
            is_realtime=is_realtime,
            model=model,
            asr_provider=asr_provider,
            asr_model=asr_model,
            llm_provider=llm_provider,
            llm_model=llm_model,
            tts_provider=tts_provider,
            tts_model=tts_model
        )

        # Adjust for requested duration if not 1 minute
        if duration_minutes != 1.0:
            for key in ["api_cost_usd", "api_cost_inr", "twilio_cost_usd", "twilio_cost_inr", "total_usd", "total_inr", "total"]:
                if key in cost_estimate:
                    cost_estimate[key] = round(cost_estimate[key] * duration_minutes, 4)

            if "breakdown" in cost_estimate:
                for key in cost_estimate["breakdown"]:
                    cost_estimate["breakdown"][key] = round(cost_estimate["breakdown"][key] * duration_minutes, 4)

        cost_estimate["estimated_duration_minutes"] = duration_minutes

        return cost_estimate

    except Exception as e:
        logger.error(f"Error estimating call cost: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error estimating call cost: {str(e)}"
        )
