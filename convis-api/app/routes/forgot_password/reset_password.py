from fastapi import APIRouter, HTTPException, Request, status
from app.middleware.rate_limiter import limiter
from app.models.reset_password import ResetPassword, ResetPasswordResponse
from app.config.database import Database
import bcrypt
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/reset-password", response_model=ResetPasswordResponse, status_code=status.HTTP_200_OK)
@limiter.limit("5/minute;20/hour")
async def reset_password(request: Request, reset_data: ResetPassword):
    """
    Reset user password using a previously-issued OTP.

    BLOCKER fix: prior implementation accepted email + new password with NO
    OTP check, allowing anyone who knew an email to reset the password.
    Now: OTP must match the one stored on the user via /send-otp, and is
    cleared after a successful reset (single-use).

    Rate-limited to slow brute-force of the 6-digit OTP space.
    """
    try:
        email = reset_data.email.strip().lower()
        db = Database.get_db()
        users_collection = db['users']

        user = users_collection.find_one({"email": email})

        # Constant-message failure for both "no user" and "wrong OTP" so we
        # don't expose enumeration through the reset surface.
        if not user or not user.get("otp") or user["otp"] != reset_data.otp:
            logger.warning("Invalid reset attempt")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid OTP",
            )

        # Hash the new password
        salt = bcrypt.gensalt(rounds=12)
        hashed_password = bcrypt.hashpw(reset_data.newPassword.encode('utf-8'), salt)

        # Atomically: replace password AND clear OTP (single-use).
        users_collection.update_one(
            {"_id": user["_id"]},
            {"$set": {"password": hashed_password.decode('utf-8')}, "$unset": {"otp": ""}},
        )

        logger.info("Password reset successful for user %s", user["_id"])
        return ResetPasswordResponse(message="Password reset successful")

    except HTTPException:
        raise
    except Exception as error:
        import traceback
        logger.error(f"Reset password error: {str(error)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server error",
        )
