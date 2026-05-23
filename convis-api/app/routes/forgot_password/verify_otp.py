from fastapi import APIRouter, HTTPException, Request, status
from app.middleware.rate_limiter import limiter
from app.models.verify_otp import VerifyOTP, VerifyOTPResponse
from app.config.database import Database
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/verify-otp", response_model=VerifyOTPResponse, status_code=status.HTTP_200_OK)
@limiter.limit("10/minute;30/hour")
async def verify_otp(request: Request, otp_data: VerifyOTP):
    """
    Verify OTP for password reset.

    Rate-limited to prevent brute-force of the 6-digit OTP space (1M codes,
    feasible in seconds without rate limit). Email is normalized.
    """
    try:
        email = otp_data.email.strip().lower()
        db = Database.get_db()
        users_collection = db['users']

        logger.info("OTP verification request received")

        user = users_collection.find_one({"email": email})

        # Constant message regardless of which side fails — no enumeration.
        if not user or user.get('otp') != otp_data.otp:
            logger.warning("Invalid OTP attempt")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid OTP"
            )

        logger.info("OTP verified successfully")

        return VerifyOTPResponse(message="OTP verified successfully")

    except HTTPException:
        raise
    except Exception as error:
        import traceback
        logger.error(f"OTP verification error: {str(error)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OTP verification failed",
        )
