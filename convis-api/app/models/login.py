from typing import Literal

from pydantic import BaseModel, EmailStr


class Login(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    redirectUrl: str
    clientId: str
    role: Literal["admin", "user"] = "user"
    token: str
