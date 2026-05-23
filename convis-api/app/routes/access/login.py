from datetime import datetime, timedelta, timezone
import logging

import bcrypt
import jwt
from fastapi import APIRouter, HTTPException, Request, Response, status

from app.config.database import Database
from app.config.settings import settings
from app.middleware.rate_limiter import limiter
from app.models.login import Login, LoginResponse
from app.utils.threadpool import run_in_thread

logger = logging.getLogger(__name__)

router = APIRouter()

# Single error message for both "no such user" and "wrong password" so we don't
# give the client a user-enumeration oracle. Timing leakage is mitigated below
# by always running a bcrypt compare even when the user doesn't exist.
_INVALID_CREDS_DETAIL = "Invalid email or password"

# A pre-computed bcrypt hash to compare against when the user is not found, so
# that response time is roughly constant regardless of whether the email exists.
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(b"unused", bcrypt.gensalt()).decode("utf-8")


@router.post("/login", response_model=LoginResponse, status_code=status.HTTP_200_OK)
@limiter.limit("10/minute;100/hour")
async def login(request: Request, login_data: Login, response: Response) -> LoginResponse:
    """User login.

    Returns a JWT carrying clientId + role. Role is the only authoritative
    source for admin checks — frontend must never self-report it.

    Rate-limited to slow brute-force / credential-stuffing attempts.
    """
    email = login_data.email.strip().lower()
    logger.info("Incoming login request: %s", email)

    db = Database.get_db()
    users_collection = db["users"]

    user = await run_in_thread(users_collection.find_one, {"email": email})

    # Always run bcrypt to keep response time constant whether or not the
    # account exists. This prevents user-enumeration via timing.
    stored_hash = (
        user["password"].encode("utf-8")
        if user and user.get("password")
        else _DUMMY_BCRYPT_HASH.encode("utf-8")
    )
    password_ok = bcrypt.checkpw(
        login_data.password.strip().encode("utf-8"),
        stored_hash,
    )

    if not user or not password_ok:
        logger.warning("Invalid login for: %s", email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDS_DETAIL,
        )

    if not user.get("verified", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email first.",
        )

    role = "admin" if user.get("role") == "admin" else "user"
    client_id = str(user["_id"])

    token_payload = {
        "email": user["email"],
        "clientId": client_id,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(days=1),
    }
    token = jwt.encode(token_payload, settings.jwt_secret, algorithm="HS256")
    logger.info("Login success: %s", email)

    is_production = settings.environment == "production"
    response.set_cookie(
        key="token",
        value=token,
        httponly=True,
        secure=is_production,
        max_age=60 * 60 * 24,
        samesite="lax",
        path="/",
    )

    return LoginResponse(
        redirectUrl=f"/client-dashboard/{client_id}",
        clientId=client_id,
        role=role,
        token=token,
    )
