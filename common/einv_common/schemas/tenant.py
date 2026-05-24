import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9\-]+$")
    confidence_threshold: float = Field(default=0.95, ge=0.5, le=1.0)
    webhook_url: str | None = None
    validation_rules: dict = Field(default_factory=dict)


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    confidence_threshold: float | None = Field(default=None, ge=0.5, le=1.0)
    webhook_url: str | None = None
    validation_rules: dict | None = None


class ApiKeyCreate(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    expires_at: datetime | None = None


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    confidence_threshold: float
    webhook_url: str | None
    is_active: bool
    created_at: datetime


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    raw_key: str  # returned once only at creation
