"""Schemas for the generic /api/v1/users router.

`UserResponse` here intentionally collides with the one in
src/schemas/healthcare.py - that collision is what produced the
`src__schemas__healthcare__UserResponse` $ref name in the original
openapi.json, and the frontend's generated types depend on it.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=100)
    full_name: str | None = None
    password: str = Field(min_length=8, max_length=128)


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    username: str | None = None
    full_name: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    is_active: bool | None = None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    username: str
    full_name: str | None = None
    role: str
    is_active: bool
    is_superuser: bool = False
    created_at: datetime
    updated_at: datetime


class HealthCheckResponse(BaseModel):
    status: str
    message: str
    timestamp: datetime
