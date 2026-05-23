from pydantic import BaseModel, EmailStr, Field


class ResetPassword(BaseModel):
    """Password reset MUST be bound to a previously-verified OTP. The client
    submits the same OTP they verified in /verify-otp; the server checks it
    matches the one stored on the user, then clears it."""
    email: EmailStr
    otp: str = Field(..., min_length=4, max_length=10)
    newPassword: str = Field(..., min_length=8)


class ResetPasswordResponse(BaseModel):
    message: str
