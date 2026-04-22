from typing import Optional, List

from pydantic import BaseModel, Field


class SignupRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8)
    email: Optional[str] = Field(
        None,
        pattern=r'^[\w\.-]+@[\w\.-]+\.\w+$',
        example="name@mail.com"
    )
    phone_number: Optional[str] = Field(
        None,
        example="+1234567890"
    )


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    username: str
    user_id: str  # CUID string


class RefreshRequest(BaseModel):
    refresh_token: str


class ProfileData(BaseModel):
    id: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None
    profile_image_url: Optional[str] = None
    date_of_birth: Optional[str] = None
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    blood_type: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    emergency_contact_relationship: Optional[str] = None
    medical_conditions: List[str] = Field(default_factory=list)
    medications: List[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PushTokenRequest(BaseModel):
    expo_push_token: str = Field(..., pattern=r'^ExponentPushToken\[.+\]$')


class UserProfileResponse(BaseModel):
    id: str  # CUID string
    username: str
    email: Optional[str] = None
    is_active: Optional[bool] = True
    last_login: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    profile: Optional[ProfileData] = None
