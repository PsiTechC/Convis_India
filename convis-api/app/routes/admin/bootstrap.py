"""One-shot bootstrap endpoint for promoting a user to the admin role.

Why this exists
---------------
The login route (`access/login.py`) only issues a JWT with `role: "admin"`
if the user document in Mongo already has `role: "admin"`. So bootstrapping
the very first admin from a fresh database creates a chicken-and-egg
problem: every other admin route requires an admin JWT, but you can't get
one without a Mongo write.

This endpoint accepts a shared secret instead of a JWT so the operator can
promote one specific user without holding admin credentials.

Security
--------
- Requires the `ADMIN_BOOTSTRAP_SECRET` env var to be set on the API
  instance. If missing, the endpoint refuses with 503 — closed by default.
- Compares the supplied secret with `secrets.compare_digest` (constant-time)
  to defeat timing attacks.
- After bootstrapping, the operator should clear `ADMIN_BOOTSTRAP_SECRET`
  from the API env vars to disable the endpoint until needed again.
- Logs every invocation (success or failure) with the request ID for audit.
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, EmailStr

from app.config.database import Database

logger = logging.getLogger(__name__)

router = APIRouter()


class BootstrapAdminRequest(BaseModel):
    email: EmailStr


class BootstrapAdminResponse(BaseModel):
    email: str
    matched: int
    modified: int
    previous_role: Optional[str]
    new_role: str = "admin"


@router.post(
    "/bootstrap-admin-role",
    response_model=BootstrapAdminResponse,
    status_code=status.HTTP_200_OK,
)
async def bootstrap_admin_role(
    body: BootstrapAdminRequest,
    x_bootstrap_secret: Optional[str] = Header(default=None, alias="X-Bootstrap-Secret"),
):
    """Set `role: "admin"` on the user matching the given email.

    Auth: the request MUST include `X-Bootstrap-Secret: <env-var-value>` —
    this is verified in constant time against `ADMIN_BOOTSTRAP_SECRET`.
    """
    expected = os.getenv("ADMIN_BOOTSTRAP_SECRET")
    if not expected:
        # Closed by default — admin must opt-in by setting the env var.
        logger.warning("[BOOTSTRAP_ADMIN] refused: ADMIN_BOOTSTRAP_SECRET not set")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bootstrap endpoint disabled. Set ADMIN_BOOTSTRAP_SECRET env var to enable.",
        )

    if not x_bootstrap_secret or not secrets.compare_digest(x_bootstrap_secret, expected):
        logger.warning("[BOOTSTRAP_ADMIN] refused: invalid secret for email=%s", body.email)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid bootstrap secret",
        )

    db = Database.get_db()
    user = db["users"].find_one({"email": str(body.email).lower()}, {"role": 1})
    if not user:
        logger.warning("[BOOTSTRAP_ADMIN] no user found for email=%s", body.email)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No user with email {body.email}",
        )

    previous_role = user.get("role")
    result = db["users"].update_one(
        {"_id": user["_id"]},
        {"$set": {"role": "admin"}},
    )

    logger.info(
        "[BOOTSTRAP_ADMIN] promoted email=%s previous_role=%s matched=%d modified=%d",
        body.email, previous_role, result.matched_count, result.modified_count,
    )

    return BootstrapAdminResponse(
        email=str(body.email),
        matched=result.matched_count,
        modified=result.modified_count,
        previous_role=previous_role,
    )
