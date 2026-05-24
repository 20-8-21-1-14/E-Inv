import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ColumnAliasOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    unmatched_header: str
    doc_type: str
    seen_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    status: str
    suggested_field: str | None
    notes: str | None


class ColumnAliasReview(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    suggested_field: str | None = None
    notes: str | None = None


class SchemaVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: str
    changelog: str | None
    is_active: bool
    created_at: datetime
    activated_at: datetime | None


class SchemaVersionCreate(BaseModel):
    version: str = Field(min_length=1, max_length=20)
    content: dict
    changelog: str | None = None


class ModelVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    model_type: str
    version: str
    minio_key: str
    metrics: dict
    training_samples: int | None
    mlflow_run_id: str | None
    is_active: bool
    created_at: datetime
    promoted_at: datetime | None
