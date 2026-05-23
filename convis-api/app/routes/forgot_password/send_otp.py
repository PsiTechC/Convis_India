from fastapi import APIRouter, HTTPException, Request, status
from app.middleware.rate_limiter import limiter
from app.models.forgot_password import SendOTP, SendOTPResponse
from app.config.database import Database
from app.utils.otp import generate_otp
from app.config.settings import settings
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

# Always-success message — prevents user enumeration via differential responses.
# An attacker probing emails sees the same response whether the account exists
# or not.
_GENERIC_OK = SendOTPResponse(
    message="If an account with that email exists, an OTP has been sent."
)


@router.post("/send-otp", response_model=SendOTPResponse, status_code=status.HTTP_200_OK)
@limiter.limit("5/minute;30/hour")
async def send_otp(request: Request, otp_data: SendOTP):
    """
    Send OTP to user's email for password reset.

    Rate-limited to 5/min, 30/hour per IP to prevent email-bombing.
    Always returns 200 with a generic message — never confirms whether
    the email exists, to prevent user enumeration.
    """
    try:
        email = otp_data.email.strip().lower()
        # Get database connection
        db = Database.get_db()
        users_collection = db['users']

        logger.info("OTP request received")  # do not log the email

        # Find user by email (normalized)
        user = users_collection.find_one({"email": email})

        if not user:
            # Same response, same timing target as the success path.
            # Don't tell the caller the account doesn't exist.
            return _GENERIC_OK

        # Generate OTP — NEVER log the OTP value.
        otp = generate_otp()

        # Update user with OTP (never log the OTP itself)
        users_collection.update_one(
            {"email": email},
            {"$set": {"otp": otp}}
        )
        logger.info("OTP saved in DB")

        # Send OTP email
        try:
            # Create message
            message = MIMEMultipart('alternative')
            message['Subject'] = 'Convis Labs Password Reset OTP'
            message['From'] = settings.email_user
            message['To'] = email

            # HTML body
            html_body = f"""
            <p>Hello,</p>
            <p>Your OTP for password reset is: <strong>{otp}</strong></p>
            <p>This OTP is valid for a short time only. If you didn't request this, please ignore this email.</p>
            """

            html_part = MIMEText(html_body, 'html')
            message.attach(html_part)

            # Connect to SMTP server
            logger.info("Connecting to SMTP server...")
            if settings.smtp_use_ssl and settings.smtp_port == 465:
                # Use SMTP_SSL for port 465
                with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as server:
                    server.login(settings.email_user, settings.email_pass)
                    server.send_message(message)
            else:
                # Use SMTP with STARTTLS for port 587
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                    server.starttls()
                    server.login(settings.email_user, settings.email_pass)
                    server.send_message(message)

            logger.info("OTP email sent successfully")

        except Exception as email_error:
            logger.error(f"Failed to send OTP email: {str(email_error)}")
            # Don't fail the request if email fails - OTP is already saved
            logger.warning("OTP saved in DB but email failed to send")

        return _GENERIC_OK

    except HTTPException:
        raise
    except Exception as error:
        import traceback
        logger.error(f"Error sending OTP: {str(error)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send OTP: {str(error)}"
        )
