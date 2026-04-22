import hashlib
import json
import logging
import os
import time
import uuid
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from pydantic import BaseModel

from auth.core import PasswordManager, JWTManager, get_current_user
from auth.models import (
    SignupRequest, LoginRequest, LoginResponse, UserProfileResponse,
    PushTokenRequest, RefreshRequest,
)

logger = logging.getLogger(__name__)

class SignupWithImeiRequest(SignupRequest):
    device_imei: Optional[str] = None


router = APIRouter()

_CLOUDINARY_CLOUD = os.getenv("CLOUDINARY_CLOUD_NAME")
_CLOUDINARY_KEY   = os.getenv("CLOUDINARY_API_KEY")
_CLOUDINARY_SECRET = os.getenv("CLOUDINARY_API_SECRET")


async def _cloudinary_upload(data: bytes, filename: str) -> str:
    """Upload image bytes to Cloudinary and return the secure URL."""
    if not (_CLOUDINARY_CLOUD and _CLOUDINARY_KEY and _CLOUDINARY_SECRET):
        raise HTTPException(status_code=500, detail="Cloudinary not configured")

    ts = int(time.time())
    name_no_ext = os.path.splitext(filename)[0]
    public_id = f"avatars/{name_no_ext}"
    params_str = f"public_id={public_id}&timestamp={ts}"
    signature = hashlib.sha1(f"{params_str}{_CLOUDINARY_SECRET}".encode()).hexdigest()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.cloudinary.com/v1_1/{_CLOUDINARY_CLOUD}/image/upload",
            data={
                "api_key": _CLOUDINARY_KEY,
                "timestamp": ts,
                "public_id": public_id,
                "signature": signature,
            },
            files={"file": (filename, data)},
        )

    if resp.status_code != 200:
        logger.error(f"Cloudinary upload failed: {resp.text}")
        raise HTTPException(status_code=500, detail="Failed to upload image to Cloudinary")

    return resp.json()["secure_url"]


async def _get_profile(user_id: str) -> Optional[Dict]:
    """Fetch profile from User table including profileImageUrl."""
    row = await get_user_manager().db.fetchrow('''
        SELECT id, "fullName", phone, email, "dateOfBirth", "heightCm", "weightKg",
               "bloodType", "medicalConditions", medications, "profileImageUrl",
               "address", "gender", "createdAt", "updatedAt"
        FROM "User" WHERE id = $1
    ''', user_id)
    if not row:
        return None
    mc = row["medicalConditions"]
    meds = row["medications"]
    if isinstance(mc, str):
        try: mc = json.loads(mc)
        except: mc = []
    if mc is None: mc = []
    if isinstance(meds, str):
        try: meds = json.loads(meds)
        except: meds = []
    if meds is None: meds = []
    return {
        "id": row["id"],
        "full_name": row["fullName"],
        "phone_number": row["phone"],
        "email": row["email"],
        "date_of_birth": row["dateOfBirth"].isoformat() if row["dateOfBirth"] else None,
        "height_cm": row["heightCm"],
        "weight_kg": row["weightKg"],
        "blood_type": row["bloodType"],
        "medical_conditions": mc,
        "medications": meds,
        "profile_image_url": row["profileImageUrl"],
        "address": row["address"],
        "gender": row["gender"],
        "created_at": row["createdAt"].isoformat() if row["createdAt"] else None,
        "updated_at": row["updatedAt"].isoformat() if row["updatedAt"] else None,
    }


def get_user_manager():
    from watch_app import user_manager
    return user_manager


class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    date_of_birth: Optional[str] = None
    gender: Optional[str] = None
    blood_type: Optional[str] = None
    height: Optional[float] = None
    weight: Optional[float] = None
    medical_conditions: Optional[list] = None
    medications: Optional[list] = None
    profile_image_url: Optional[str] = None
    address: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None


@router.post("/api/auth/signup", tags=["auth"], summary="Create a new user account", response_model=LoginResponse)
async def signup(body: SignupWithImeiRequest):
    password_hash = PasswordManager.hash_password(body.password)

    # Validate IMEI exists in the Device table before creating account
    if body.device_imei:
        imei_row = await get_user_manager().db.fetchval(
            'SELECT id FROM "Device" WHERE "deviceId" = $1', body.device_imei
        )
        if not imei_row:
            raise HTTPException(status_code=400, detail="IMEI not registered")

    # Check for existing account with this email
    existing = await get_user_manager().get_user_by_email(body.email) if body.email else None

    if existing:
        if existing.get("isActive", True):
            raise HTTPException(status_code=400, detail="Email already registered")
        # Inactive account — reactivate it with the new credentials
        try:
            await get_user_manager().db.execute(
                'UPDATE "User" SET "username" = $1, "passwordHash" = $2, "isActive" = true, "updatedAt" = NOW() WHERE id = $3',
                body.username, password_hash, existing["id"]
            )
            await get_user_manager().update_last_login(existing["id"])
            token_data = {"user_id": existing["id"], "username": body.username}
            access_token = JWTManager.create_access_token(token_data)
            refresh_token = JWTManager.create_refresh_token(token_data)
            return LoginResponse(
                access_token=access_token,
                refresh_token=refresh_token,
                username=body.username,
                user_id=existing["id"]
            )
        except Exception as e:
            logger.error(f"Reactivation error: {e}")
            raise HTTPException(status_code=500, detail="Failed to reactivate account")

    try:
        default_profile = {
            "full_name": body.username,
            "email": body.email,
            "phone_number": body.phone_number,
            "medical_conditions": [],
            "medications": []
        }

        user_id = await get_user_manager().create_user(
            username=body.username,
            password_hash=password_hash,
            email=body.email,
            profile_data=default_profile
        )

        token_data = {"user_id": user_id, "username": body.username}
        access_token = JWTManager.create_access_token(token_data)
        refresh_token = JWTManager.create_refresh_token(token_data)

        await get_user_manager().update_last_login(user_id)

        # Link device to the new user if IMEI was provided
        if body.device_imei:
            try:
                await get_user_manager().link_device_to_user(
                    user_id=user_id,
                    device_id=body.device_imei,
                    device_type='PENDANT',
                )
            except Exception as link_err:
                logger.warning(f"Device link failed after signup for {user_id}: {link_err}")

        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            username=body.username,
            user_id=user_id
        )
    except Exception as e:
        logger.error(f"Signup error: {e}")
        raise HTTPException(status_code=500, detail="Failed to create user account")


@router.post("/api/auth/login", tags=["auth"], summary="Login to get JWT token", response_model=LoginResponse)
async def login(body: LoginRequest):
    user = await get_user_manager().get_user_by_email(body.email)

    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not PasswordManager.verify_password(body.password, user["passwordHash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.get("isActive", True):
        raise HTTPException(status_code=403, detail="User account is disabled")

    token_data = {"user_id": user["id"], "username": user["username"]}
    access_token = JWTManager.create_access_token(token_data)
    refresh_token = JWTManager.create_refresh_token(token_data)

    await get_user_manager().update_last_login(user["id"])

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        username=user["username"],
        user_id=user["id"]
    )


@router.get("/api/auth/me", tags=["auth"], summary="Get current user information", response_model=UserProfileResponse)
async def get_current_user_info(current_user: Dict = Depends(get_current_user)):
    user = await get_user_manager().get_user_by_id(current_user["user_id"])

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.pop("passwordHash", None)

    profile = await _get_profile(user["id"])
    user["profile"] = profile

    return user


_FIELD_TO_COLUMN = {
    "full_name":              "fullName",
    "phone_number":           "phone",
    "date_of_birth":          "dateOfBirth",
    "gender":                 "gender",
    "blood_type":             "bloodType",
    "height":                 "heightCm",
    "weight":                 "weightKg",
    "medical_conditions":     "medicalConditions",
    "medications":            "medications",
    "profile_image_url":      "profileImageUrl",
    "address":                "address",
}
_JSON_FIELDS = {"medicalConditions", "medications"}


@router.patch("/api/auth/me", tags=["auth"], summary="Update current user profile")
async def update_profile(
    body: UpdateProfileRequest,
    current_user: Dict = Depends(get_current_user),
):
    user_id = current_user["user_id"]
    raw = {k: v for k, v in body.dict().items() if v is not None}

    column_updates = {}
    for field, val in raw.items():
        col = _FIELD_TO_COLUMN.get(field)
        if col:
            column_updates[col] = json.dumps(val) if col in _JSON_FIELDS and isinstance(val, (list, dict)) else val

    if not column_updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    try:
        set_clauses = []
        values = []
        for i, (col, val) in enumerate(column_updates.items(), start=1):
            set_clauses.append(f'"{col}" = ${i}')
            values.append(val)
        values.append(user_id)
        query = f'UPDATE "User" SET {", ".join(set_clauses)}, "updatedAt" = NOW() WHERE id = ${len(values)}'
        await get_user_manager().db.execute(query, *values)

        updated_profile = await _get_profile(user_id)
        return {"success": True, "message": "Profile updated successfully", "profile": updated_profile}

    except Exception as e:
        logger.error(f"Update profile error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update profile: {str(e)}")


@router.get("/api/auth/avatar/sign", tags=["auth"], summary="Get Cloudinary upload signature")
async def get_avatar_upload_signature(current_user: Dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    if not (_CLOUDINARY_CLOUD and _CLOUDINARY_KEY and _CLOUDINARY_SECRET):
        raise HTTPException(status_code=500, detail="Cloudinary not configured")
    ts = int(time.time())
    public_id = f"avatars/{user_id}_{uuid.uuid4().hex[:8]}"
    params_str = f"public_id={public_id}&timestamp={ts}"
    signature = hashlib.sha1(f"{params_str}{_CLOUDINARY_SECRET}".encode()).hexdigest()
    return {
        "cloud_name": _CLOUDINARY_CLOUD,
        "api_key": _CLOUDINARY_KEY,
        "timestamp": ts,
        "public_id": public_id,
        "signature": signature,
    }


class AvatarUrlRequest(BaseModel):
    url: str

@router.post("/api/auth/avatar/url", tags=["auth"], summary="Save avatar URL after direct Cloudinary upload")
async def save_avatar_url(body: AvatarUrlRequest, current_user: Dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    if not body.url or not body.url.startswith("https://res.cloudinary.com/"):
        raise HTTPException(status_code=400, detail="Invalid Cloudinary URL")
    try:
        await get_user_manager().db.execute(
            'UPDATE "User" SET "profileImageUrl" = $1, "updatedAt" = NOW() WHERE id = $2',
            body.url, user_id
        )
    except Exception as e:
        logger.error(f"Avatar URL save error: {e}")
        raise HTTPException(status_code=500, detail="Failed to save avatar URL")
    return {"success": True, "url": body.url}


@router.post("/api/auth/avatar", tags=["auth"], summary="Upload profile avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: Dict = Depends(get_current_user),
):
    user_id = current_user["user_id"]
    ext = os.path.splitext(file.filename or "avatar.jpg")[1].lower() or ".jpg"
    filename = f"{user_id}_{uuid.uuid4().hex[:8]}{ext}"

    data = await file.read()
    url = await _cloudinary_upload(data, filename)

    try:
        await get_user_manager().db.execute(
            'UPDATE "User" SET "profileImageUrl" = $1, "updatedAt" = NOW() WHERE id = $2',
            url, user_id
        )
    except Exception as e:
        logger.error(f"Avatar DB update error: {e}")
        raise HTTPException(status_code=500, detail="Failed to update avatar URL in database")

    return {"success": True, "url": url}


@router.post("/api/auth/refresh", tags=["auth"], summary="Refresh access token", response_model=LoginResponse)
async def refresh_token(body: RefreshRequest):
    payload = JWTManager.decode_refresh_token(body.refresh_token)

    user_id = payload.get("user_id")
    username = payload.get("username")

    if not user_id or not username:
        raise HTTPException(status_code=401, detail="Invalid refresh token payload")

    user = await get_user_manager().get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.get("isActive", True):
        raise HTTPException(status_code=403, detail="User account is disabled")

    token_data = {"user_id": user_id, "username": username}
    new_access_token = JWTManager.create_access_token(token_data)
    new_refresh_token = JWTManager.create_refresh_token(token_data)

    return LoginResponse(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        username=username,
        user_id=user_id,
    )


@router.delete("/api/auth/me", tags=["auth"], summary="Delete (deactivate) user account")
async def delete_account(current_user: Dict = Depends(get_current_user)):
    try:
        user = await get_user_manager().get_user_by_id(current_user["user_id"])
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        await get_user_manager().deactivate_user(current_user["user_id"])

        return {"detail": "Account has been deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Account deletion error: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete account")


@router.put("/api/auth/push-token", tags=["auth"], summary="Register Expo push token")
async def register_push_token(
    body: PushTokenRequest,
    current_user: Dict = Depends(get_current_user)
):
    await get_user_manager().update_push_token(
        current_user["user_id"],
        body.expo_push_token,
    )
    return {"detail": "Push token registered"}
