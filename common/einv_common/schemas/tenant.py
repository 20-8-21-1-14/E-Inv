import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _validate_quota_pair(quota_max_docs, quota_window_seconds) -> None:
    """Both fields must be set together or both null."""
    if (quota_max_docs is None) != (quota_window_seconds is None):
        raise ValueError(
            "quota_max_docs and quota_window_seconds must both be set or both be null"
        )
    if quota_max_docs is not None and quota_max_docs <= 0:
        raise ValueError("quota_max_docs must be > 0")
    if quota_window_seconds is not None and not (0 < quota_window_seconds <= 2_592_000):
        raise ValueError("quota_window_seconds must be between 1 and 2592000 (30 days)")


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9\-]+$")
    confidence_threshold: float = Field(default=0.95, ge=0.5, le=1.0)
    webhook_url: str | None = None
    quota_max_docs: int | None = None
    quota_window_seconds: int | None = None
    validation_rules: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_quota_pair(self) -> "TenantCreate":
        _validate_quota_pair(self.quota_max_docs, self.quota_window_seconds)
        return self


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    confidence_threshold: float | None = Field(default=None, ge=0.5, le=1.0)
    webhook_url: str | None = None
    quota_max_docs: Optional[int] = None
    quota_window_seconds: Optional[int] = None
    validation_rules: dict | None = None

    @model_validator(mode="after")
    def check_quota_pair(self) -> "TenantUpdate":
        if self.quota_max_docs is not None or self.quota_window_seconds is not None:
            _validate_quota_pair(self.quota_max_docs, self.quota_window_seconds)
        return self


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
    quota_max_docs: int | None
    quota_window_seconds: int | None
    is_active: bool
    created_at: datetime


class TenantCreated(TenantOut):
    """Returned once at tenant creation — includes plaintext webhook_secret."""
    webhook_secret: str


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
