"""
Authentication utilities for JWT token verification
"""
import logging
from typing import Optional
from fastapi import Header, HTTPException, Request, status
import jwt
from bson import ObjectId
from app.config.settings import settings
from app.config.database import Database

logger = logging.getLogger(__name__)


async def get_current_user(
    request: Request,
    authorization: str = Header(None),
) -> dict:
    """
    Verify JWT token and return current user

    Token sources (in priority order):
      1. `Authorization: Bearer <jwt>` header — used by all programmatic API calls
      2. `?token=<jwt>` query param — used by HTML elements like `<audio src=...>`
         that can't set custom headers. The recording proxy endpoint relies on
         this so the browser's native audio player works.
      3. `token` cookie (set on login) — fallback for same-site browser nav

    Args:
        request: incoming FastAPI request (for query/cookie token fallback)
        authorization: Bearer token from Authorization header

    Returns:
        dict: User document from database

    Raises:
        HTTPException: If token is missing, invalid, or user not found
    """
    token = None
    if authorization and authorization.startswith('Bearer '):
        token = authorization[len('Bearer '):]
    if not token:
        # Allow query-param fallback for browser-native fetchers (audio/img tags).
        token = request.query_params.get('token')
    if not token:
        # Fallback to httponly cookie set at login.
        token = request.cookies.get('token')

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization token"
        )

    try:
        # Decode JWT token
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        user_id = payload.get('clientId')

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload"
            )

        # Get user from database
        db = Database.get_db()
        users_collection = db['users']

        try:
            user_obj_id = ObjectId(user_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user ID in token"
            )

        user = users_collection.find_one({"_id": user_obj_id})

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found"
            )

        # Add user_id as string for convenience
        user['user_id'] = str(user['_id'])
        # Authoritative role comes from the signed JWT, not the user document.
        # Older tokens without role default to "user" — never to admin.
        token_role = payload.get('role')
        if token_role not in ("admin", "user"):
            token_role = "user"
        user['token_role'] = token_role

        return user

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired"
        )
    except jwt.PyJWTError as e:
        logger.error(f"JWT decode error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )


async def verify_user_ownership(user: dict, resource_user_id: str) -> None:
    """
    Verify that the authenticated user owns the resource. Admins (per the
    JWT role claim) may access any resource.

    Args:
        user: Current authenticated user (from get_current_user)
        resource_user_id: User ID that owns the resource

    Raises:
        HTTPException: If user does not own the resource
    """
    if user.get('token_role') == 'admin':
        return
    if user['user_id'] != resource_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this resource"
        )


def require_admin(user: dict) -> None:
    """Raise 403 unless the JWT carried role=admin. Use this on admin-only
    endpoints; never trust a client-supplied admin flag."""
    if user.get('token_role') != 'admin':
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
