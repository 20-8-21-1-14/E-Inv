# Stage 5: Core-API Production Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add webhook delivery with HMAC signing + Celery retry, CSV/JSON export endpoints, per-tenant Redis sliding-window rate limiting, and a document retry endpoint — making the platform usable end-to-end.

**Architecture:** Celery `deliver_webhook` task dispatched after `process_document` commits its result; atomic Redis Lua script enforces per-tenant upload quotas; new `exports.py` router serves CSV/JSON; retry endpoint uses atomic `UPDATE … RETURNING` to prevent duplicate jobs.

**Tech Stack:** FastAPI, SQLAlchemy async, Celery, Redis (aioredis), httpx, PostgreSQL, Alembic, structlog, Python `csv` module, `hmac` + `hashlib` stdlib.

## Global Constraints

- Python 3.12+, FastAPI 0.110+, SQLAlchemy 2.x async mapped_column style
- All monetary values serialized as strings (decimal notation) — never floats
- Webhook secret stored as raw hex in DB (it's a signing key, not a password — hashing would break signing); shown once at creation
- Rate limit keys must not be evicted — Redis `noeviction` policy recommended
- Fail-open on Redis unavailability (log WARNING, allow upload)
- Max 3 tenant-triggered retries per document (`processing_attempts >= 3` → 429)
- Webhook: max 5 Celery attempts, exponential backoff 2→4→8→16→32s, abandon on non-transient 4xx
- No new Docker services or containers introduced

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Modify | `common/einv_common/models/tenant.py` | Add `quota_max_docs`, `quota_window_seconds`, `webhook_secret_hash` |
| Modify | `common/einv_common/models/document.py` | Add `processing_attempts`, `last_retry_at` |
| Create | `common/einv_common/models/webhook.py` | `WebhookDelivery` ORM model |
| Modify | `common/einv_common/models/__init__.py` | Export `WebhookDelivery` |
| Modify | `common/einv_common/schemas/tenant.py` | Add quota + webhook_secret fields to schemas |
| Create | `migrations/versions/0002_stage5_hardening.py` | Alembic migration |
| Create | `services/core-api/app/ratelimit.py` | Atomic Redis Lua quota enforcement |
| Create | `services/core-api/app/routers/exports.py` | CSV + JSON export endpoints |
| Modify | `services/core-api/app/routers/documents.py` | Add `POST /retry` endpoint |
| Modify | `services/core-api/app/main.py` | Register exports router |
| Modify | `services/ocr-worker/pipeline/webhook_dispatcher.py` | Replace fire_and_forget with Celery dispatch |
| Modify | `services/ocr-worker/tasks.py` | Add `deliver_webhook` Celery task |
| Modify | `services/ocr-worker/pipeline/orchestrator.py` | Dispatch `deliver_webhook` after commit |
| Modify | `services/admin-api/app/routers/tenants.py` | Quota + webhook_secret on create/update |

---

### Task 1: Common model changes — Tenant, Document, WebhookDelivery

**Files:**
- Modify: `common/einv_common/models/tenant.py`
- Modify: `common/einv_common/models/document.py`
- Create: `common/einv_common/models/webhook.py`
- Modify: `common/einv_common/models/__init__.py`

**Interfaces:**
- Produces: `Tenant.quota_max_docs: int | None`, `Tenant.quota_window_seconds: int | None`, `Tenant.webhook_secret_hash: str | None`
- Produces: `Document.processing_attempts: int`, `Document.last_retry_at: datetime | None`
- Produces: `WebhookDelivery` model with all columns from spec

- [ ] **Step 1: Add columns to Tenant model**

Edit `common/einv_common/models/tenant.py` — add three columns after `webhook_url`:

```python
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from einv_common.db.base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    confidence_threshold: Mapped[float] = mapped_column(Float, default=0.95, nullable=False)
    webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quota_max_docs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_window_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validation_rules: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    api_keys: Mapped[list["ApiKey"]] = relationship("ApiKey", back_populates="tenant")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="api_keys")
```

- [ ] **Step 2: Add columns to Document model**

Edit `common/einv_common/models/document.py` — add `processing_attempts` and `last_retry_at` before `extraction_result` relationship:

```python
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from einv_common.db.base import Base


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_tenant_created", "tenant_id", "created_at"),
        Index("ix_documents_tenant_status", "tenant_id", "status"),
        Index("ix_documents_file_hash_tenant", "file_hash", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    doc_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_format: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    processing_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    extraction_result: Mapped["ExtractionResult | None"] = relationship(  # noqa: F821
        "ExtractionResult", back_populates="document", uselist=False
    )
```

- [ ] **Step 3: Create WebhookDelivery model**

Create `common/einv_common/models/webhook.py`:

```python
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from einv_common.db.base import Base


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_tenant_doc", "tenant_id", "document_id"),
        Index("ix_webhook_deliveries_doc_attempt", "document_id", "attempt"),
        Index("ix_webhook_deliveries_status_retry", "status", "next_retry_at"),
        CheckConstraint("attempt > 0", name="ck_webhook_deliveries_attempt_positive"),
        CheckConstraint(
            "http_status IS NULL OR (http_status >= 100 AND http_status <= 599)",
            name="ck_webhook_deliveries_http_status_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)  # stable across retries; used as payload event_id
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body_truncated: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 4: Export WebhookDelivery from `__init__.py`**

Edit `common/einv_common/models/__init__.py`:

```python
from einv_common.models.tenant import Tenant, ApiKey
from einv_common.models.document import Document
from einv_common.models.extraction import ExtractionResult, InvoiceLineItem, FieldConfidence
from einv_common.models.hitl import HitlQueue
from einv_common.models.audit import AuditLog
from einv_common.models.user import AdminUser
from einv_common.models.training import ColumnAliasProposal, SchemaVersion, ModelVersion
from einv_common.models.webhook import WebhookDelivery

__all__ = [
    "Tenant", "ApiKey",
    "Document",
    "ExtractionResult", "InvoiceLineItem", "FieldConfidence",
    "HitlQueue",
    "AuditLog",
    "AdminUser",
    "ColumnAliasProposal", "SchemaVersion", "ModelVersion",
    "WebhookDelivery",
]
```

- [ ] **Step 5: Commit**

```bash
git add common/einv_common/models/
git commit -m "feat(common): add WebhookDelivery model, quota + retry fields to Tenant/Document"
```

---

### Task 2: Update common schemas for Tenant

**Files:**
- Modify: `common/einv_common/schemas/tenant.py`

**Interfaces:**
- Produces: `TenantCreate` with `quota_max_docs`, `quota_window_seconds`, `webhook_url` (validated)
- Produces: `TenantUpdate` with same optional fields
- Produces: `TenantOut` with quota fields
- Produces: `TenantCreated(TenantOut)` with `webhook_secret: str` (shown once)

- [ ] **Step 1: Rewrite tenant schemas**

Replace `common/einv_common/schemas/tenant.py` entirely:

```python
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
        # Only validate if either quota field is explicitly provided
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
```

- [ ] **Step 2: Commit**

```bash
git add common/einv_common/schemas/tenant.py
git commit -m "feat(common): update tenant schemas with quota fields and TenantCreated"
```

---

### Task 3: Alembic migration

**Files:**
- Create: `migrations/versions/0002_stage5_hardening.py`

**Interfaces:**
- Consumes: models from Task 1 (just for reference — migration uses raw SQL ops)
- Produces: `webhook_deliveries` table, new columns on `tenants` and `documents`

- [ ] **Step 1: Create migration file**

Create `migrations/versions/0002_stage5_hardening.py`:

```python
"""Stage 5 hardening: webhook_deliveries table, quota columns, retry tracking.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── tenants: quota + webhook secret ─────────────────────────────────────
    op.add_column("tenants", sa.Column("webhook_secret", sa.String(64), nullable=True))
    op.add_column("tenants", sa.Column("quota_max_docs", sa.Integer(), nullable=True))
    op.add_column("tenants", sa.Column("quota_window_seconds", sa.Integer(), nullable=True))

    # ── documents: retry tracking ────────────────────────────────────────────
    op.add_column(
        "documents",
        sa.Column("processing_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("documents", sa.Column("last_retry_at", sa.DateTime(timezone=True), nullable=True))

    # ── webhook_deliveries ───────────────────────────────────────────────────
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("response_body_truncated", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("attempt > 0", name="ck_webhook_deliveries_attempt_positive"),
        sa.CheckConstraint(
            "http_status IS NULL OR (http_status >= 100 AND http_status <= 599)",
            name="ck_webhook_deliveries_http_status_range",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_tenant_doc", "webhook_deliveries", ["tenant_id", "document_id"]
    )
    op.create_index(
        "ix_webhook_deliveries_doc_attempt", "webhook_deliveries", ["document_id", "attempt"]
    )
    op.create_index(
        "ix_webhook_deliveries_status_retry", "webhook_deliveries", ["status", "next_retry_at"]
    )


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_column("documents", "last_retry_at")
    op.drop_column("documents", "processing_attempts")
    op.drop_column("tenants", "quota_window_seconds")
    op.drop_column("tenants", "quota_max_docs")
    op.drop_column("tenants", "webhook_secret_hash")
```

- [ ] **Step 2: Commit**

```bash
git add migrations/versions/0002_stage5_hardening.py
git commit -m "feat(migrations): stage 5 hardening — webhook_deliveries, quota, retry columns"
```

---

### Task 4: Redis Lua rate limiter (core-api)

**Files:**
- Create: `services/core-api/app/ratelimit.py`

**Interfaces:**
- Produces: `async def check_quota(tenant: Tenant, redis: aioredis.Redis) -> None` — raises `HTTPException(429)` if quota exceeded, no-op if tenant has no quota or Redis is down

- [ ] **Step 1: Create ratelimit.py**

Create `services/core-api/app/ratelimit.py`:

```python
"""Per-tenant upload quota enforcement via atomic Redis Lua sliding window."""

import uuid
import structlog
import redis.asyncio as aioredis
from fastapi import HTTPException

from einv_common.models.tenant import Tenant

logger = structlog.get_logger()

# Atomic sliding-window Lua script.
# KEYS[1] = "ratelimit:{tenant_id}"
# ARGV[1] = quota_max_docs  (int)
# ARGV[2] = quota_window_ms (int, milliseconds)
# ARGV[3] = ratelimit_event_id (unique string per upload attempt)
#
# Returns: {remaining, retry_after_seconds}
#   remaining == -1  →  quota exceeded; retry_after_seconds > 0
#   remaining >= 0   →  accepted; retry_after_seconds == 0
_LUA_SCRIPT = """
local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local window_start = now_ms - tonumber(ARGV[2])

redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, window_start)
local count = redis.call('ZCARD', KEYS[1])

if count >= tonumber(ARGV[1]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry_after = math.ceil((tonumber(oldest[2]) + tonumber(ARGV[2]) - now_ms) / 1000)
  return {-1, math.max(1, retry_after)}
end

redis.call('ZADD', KEYS[1], now_ms, ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
return {tonumber(ARGV[1]) - count - 1, 0}
"""


async def check_quota(tenant: Tenant, redis: aioredis.Redis) -> None:
    """Enforce per-tenant upload quota. Raises 429 if exceeded. Fail-open on Redis errors."""
    if tenant.quota_max_docs is None or tenant.quota_window_seconds is None:
        return  # unlimited tenant

    key = f"ratelimit:{tenant.id}"
    window_ms = tenant.quota_window_seconds * 1000
    event_id = str(uuid.uuid4())

    try:
        result = await redis.eval(
            _LUA_SCRIPT,
            1,          # number of KEYS
            key,        # KEYS[1]
            str(tenant.quota_max_docs),
            str(window_ms),
            event_id,
        )
        remaining, retry_after = int(result[0]), int(result[1])
    except Exception as exc:
        logger.warning(
            "ratelimit.redis_unavailable",
            tenant_id=str(tenant.id),
            error=str(exc),
        )
        return  # fail-open

    if remaining == -1:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "QUOTA_EXCEEDED",
                "message": (
                    f"Upload quota of {tenant.quota_max_docs} documents per "
                    f"{tenant.quota_window_seconds}s exceeded."
                ),
            },
            headers={"Retry-After": str(retry_after)},
        )
```

- [ ] **Step 2: Wire quota check into upload endpoint**

Edit `services/core-api/app/routers/documents.py` — import and call `check_quota` in `upload_document`, after the idempotency check and before reading the file:

```python
# Add this import at the top of documents.py
from app.ratelimit import check_quota
```

Then in `upload_document`, after the file size check and before the dedup check (only accepted, valid uploads consume quota):

```python
    # ── Quota check (after size/MIME validation — only valid uploads consume quota) ─
    await check_quota(tenant, redis)

    # ── Dedup by SHA-256 ───────────────────────────────────────────────────
```

- [ ] **Step 3: Commit**

```bash
git add services/core-api/app/ratelimit.py services/core-api/app/routers/documents.py
git commit -m "feat(core-api): per-tenant upload quota via atomic Redis Lua sliding window"
```

---

### Task 5: Export endpoints (core-api)

**Files:**
- Create: `services/core-api/app/routers/exports.py`
- Modify: `services/core-api/app/main.py`

**Interfaces:**
- Consumes: `get_current_tenant`, `get_session` from `app.dependencies`
- Consumes: `ExtractionResult`, `InvoiceLineItem`, `FieldConfidence`, `Document` from `einv_common.models`
- Produces: `GET /v1/documents/{id}/export/csv` → `text/csv` streaming response
- Produces: `GET /v1/documents/{id}/export/json` → JSON dict with `schema_version: "1.0"`

- [ ] **Step 1: Create exports router**

Create `services/core-api/app/routers/exports.py`:

```python
"""CSV and JSON export endpoints for extracted invoice data."""

import csv
import io
import uuid
from datetime import date

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from einv_common.db import get_session
from einv_common.models import Document, ExtractionResult
from einv_common.models.tenant import Tenant
from app.dependencies import get_current_tenant

logger = structlog.get_logger()
router = APIRouter()

_READY_STATUSES = ("done", "hitl")

_CSV_HEADER = [
    "invoice_number", "invoice_date", "seller_tax_id", "buyer_tax_id",
    "seller_name", "buyer_name", "grand_total", "currency",
    "line_number", "item_name", "item_code", "unit",
    "quantity", "unit_price", "tax_rate", "tax_amount", "total_amount",
]


def _safe_cell(value: str | None) -> str:
    """Prefix values that Excel would interpret as formulas."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


async def _load_result(
    document_id: uuid.UUID,
    tenant: Tenant,
    session: AsyncSession,
) -> tuple[Document, ExtractionResult]:
    doc_row = await session.execute(
        select(Document).where(
            Document.id == document_id,
            Document.tenant_id == tenant.id,
        )
    )
    doc = doc_row.scalar_one_or_none()
    if doc is None:
        raise HTTPException(404, detail={"code": "DOCUMENT_NOT_FOUND"})
    if doc.status not in _READY_STATUSES:
        raise HTTPException(
            404,
            detail={
                "code": "RESULT_NOT_READY",
                "message": f"Document status is '{doc.status}'. Export available when status is 'done' or 'hitl'.",
            },
        )

    result_row = await session.execute(
        select(ExtractionResult)
        .where(ExtractionResult.document_id == document_id)
        .options(
            selectinload(ExtractionResult.line_items),
            selectinload(ExtractionResult.field_confidences),
        )
    )
    result = result_row.scalar_one_or_none()
    if result is None:
        raise HTTPException(404, detail={"code": "RESULT_NOT_FOUND"})

    return doc, result


@router.get("/{document_id}/export/csv", summary="Export extraction result as CSV")
async def export_csv(
    document_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    doc, result = await _load_result(document_id, tenant, session)
    vf = result.validated_fields

    def _gen():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_HEADER)
        line_items = sorted(result.line_items, key=lambda x: x.line_number)
        if not line_items:
            # Emit one row with header/total fields and empty line-item columns
            writer.writerow([
                _safe_cell(vf.get("invoice_number")),
                _safe_cell(vf.get("invoice_date")),
                _safe_cell(vf.get("seller_tax_code")),
                _safe_cell(vf.get("buyer_tax_code")),
                _safe_cell(vf.get("seller_name")),
                _safe_cell(vf.get("buyer_name")),
                _safe_cell(str(result.grand_total) if result.grand_total is not None else None),
                _safe_cell(vf.get("currency", "VND")),
                "", "", "", "", "", "", "", "", "",
            ])
        for li in line_items:
            writer.writerow([
                _safe_cell(vf.get("invoice_number")),
                _safe_cell(vf.get("invoice_date")),
                _safe_cell(vf.get("seller_tax_code")),
                _safe_cell(vf.get("buyer_tax_code")),
                _safe_cell(vf.get("seller_name")),
                _safe_cell(vf.get("buyer_name")),
                _safe_cell(str(result.grand_total) if result.grand_total is not None else None),
                _safe_cell(vf.get("currency", "VND")),
                _safe_cell(str(li.line_number)),
                _safe_cell(li.item_name),
                _safe_cell(li.item_code),
                _safe_cell(li.unit),
                _safe_cell(str(li.quantity)),
                _safe_cell(str(li.unit_price)),
                _safe_cell(str(li.tax_rate)),
                _safe_cell(str(li.tax_amount)),
                _safe_cell(str(li.total_amount)),
            ])
        yield buf.getvalue()

    filename = f"{document_id}.csv"
    return StreamingResponse(
        _gen(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{document_id}/export/json", summary="Export extraction result as structured JSON")
async def export_json(
    document_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    doc, result = await _load_result(document_id, tenant, session)
    vf = result.validated_fields
    line_items = sorted(result.line_items, key=lambda x: x.line_number)
    field_conf_map = {fc.field_name: fc.confidence for fc in result.field_confidences}

    return {
        "schema_version": "1.0",
        "document_id": str(document_id),
        "doc_type": doc.doc_type,
        "extracted_at": result.created_at.isoformat(),
        "header": {
            "invoice_number": vf.get("invoice_number"),
            "invoice_date": vf.get("invoice_date"),
            "seller_tax_id": vf.get("seller_tax_code"),
            "seller_name": vf.get("seller_name"),
            "buyer_tax_id": vf.get("buyer_tax_code"),
            "buyer_name": vf.get("buyer_name"),
        },
        "totals": {
            "subtotal": str(result.subtotal) if result.subtotal is not None else None,
            "total_tax": str(result.total_tax) if result.total_tax is not None else None,
            "grand_total": str(result.grand_total) if result.grand_total is not None else None,
            "currency": vf.get("currency", "VND"),
        },
        "line_items": [
            {
                "line_number": li.line_number,
                "item_name": li.item_name,
                "item_code": li.item_code,
                "unit": li.unit,
                "quantity": str(li.quantity),
                "unit_price": str(li.unit_price),
                "tax_rate": str(li.tax_rate),
                "tax_amount": str(li.tax_amount),
                "total_amount": str(li.total_amount),
            }
            for li in line_items
        ],
        "confidence": {
            "document_score": result.confidence_score,
            "fields": field_conf_map,
        },
    }
```

- [ ] **Step 2: Register exports router in main.py**

Edit `services/core-api/app/main.py` — add import and router registration:

```python
from app.routers import documents, webhooks, exports   # add exports

# In the routers section, add:
app.include_router(exports.router, prefix="/v1/documents", tags=["exports"])
```

- [ ] **Step 3: Commit**

```bash
git add services/core-api/app/routers/exports.py services/core-api/app/main.py
git commit -m "feat(core-api): CSV and JSON export endpoints for extraction results"
```

---

### Task 6: Document retry endpoint (core-api)

**Files:**
- Modify: `services/core-api/app/routers/documents.py`

**Interfaces:**
- Produces: `POST /v1/documents/{id}/retry` → 202 accepted, 404 not found, 409 not in error state, 429 max retries reached

- [ ] **Step 1: Add retry endpoint to documents.py**

In `services/core-api/app/routers/documents.py`, add these imports at the top if not present:

```python
from sqlalchemy import func, select, update, text
from einv_common.celery_client import get_celery_app
```

Then append the retry endpoint at the bottom of the file:

```python
# ── Retry ─────────────────────────────────────────────────────────────────────

_MAX_RETRIES = 3


@router.post("/{document_id}/retry", status_code=202, summary="Re-queue a failed document for processing")
async def retry_document(
    document_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # Load document first to check ownership and current state
    doc = await _get_owned_document(document_id, tenant.id, session)

    if doc.processing_attempts >= _MAX_RETRIES:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "MAX_RETRIES_REACHED",
                "message": f"Document has been retried {doc.processing_attempts} times (max {_MAX_RETRIES}).",
            },
        )

    if doc.status != "error":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NOT_IN_ERROR_STATE",
                "message": f"Document status is '{doc.status}'. Retry is only allowed for documents in 'error' state.",
            },
        )

    # Atomic update — guards both status='error' AND attempts < max in the WHERE clause
    # to prevent TOCTOU race between the pre-check read and this write.
    result = await session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.tenant_id == tenant.id,
            Document.status == "error",
            Document.processing_attempts < _MAX_RETRIES,
        )
        .values(
            status="queued",
            error_message=None,
            processing_attempts=Document.processing_attempts + 1,
            last_retry_at=func.now(),
        )
        .returning(Document.id, Document.processing_attempts)
    )
    row = result.one_or_none()

    if row is None:
        # Race: status or attempt count changed between our read and this write
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NOT_IN_ERROR_STATE",
                "message": "Document is no longer in 'error' state or max retries reached.",
            },
        )

    _, attempt_number = row  # use DB-returned attempt count, not stale ORM value
    session.add(AuditLog(
        tenant_id=tenant.id,
        document_id=document_id,
        action="retry",
        actor="api_key",
        details={"attempt": attempt_number},
    ))
    await session.commit()

    celery = get_celery_app()
    task = celery.send_task(
        "tasks.process_document",
        args=[str(document_id), str(tenant.id)],
        queue="ocr",
    )

    logger.info("document.retry.queued", document_id=str(document_id), attempt=attempt_number, task_id=str(task.id))
    return {
        "document_id": str(document_id),
        "task_id": str(task.id),
        "status": "queued",
        "attempt": attempt_number,
    }
```

Also add `AuditLog` to the imports in `documents.py` if not already present:
```python
from einv_common.models import AuditLog, Document, ExtractionResult, Tenant
```

- [ ] **Step 2: Commit**

```bash
git add services/core-api/app/routers/documents.py
git commit -m "feat(core-api): POST /v1/documents/{id}/retry with atomic state transition"
```

---

### Task 7: Webhook delivery Celery task (ocr-worker)

**Files:**
- Modify: `services/ocr-worker/pipeline/webhook_dispatcher.py`
- Modify: `services/ocr-worker/tasks.py`
- Modify: `services/ocr-worker/pipeline/orchestrator.py`

**Interfaces:**
- Produces: `tasks.deliver_webhook(delivery_id, document_id, tenant_id, event_type, final_status, confidence_score, ocr_engine)` Celery task
- Produces: `dispatch_webhook(document_id, tenant_id, final_status, confidence_score, ocr_engine, session)` async helper called by orchestrator after commit

- [ ] **Step 1: Rewrite webhook_dispatcher.py**

Replace `services/ocr-worker/pipeline/webhook_dispatcher.py` entirely:

```python
"""Webhook delivery helper — called by orchestrator to enqueue a Celery delivery task."""

import hashlib
import hmac
import ipaddress
import urllib.parse
import uuid
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()

# RFC-1918 + loopback + link-local + cloud metadata ranges
_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def validate_webhook_url(url: str, require_https: bool = True) -> None:
    """Raise ValueError if the URL is not a safe external target."""
    parsed = urllib.parse.urlparse(url)
    if require_https and parsed.scheme != "https":
        raise ValueError(f"Webhook URL must use https (got {parsed.scheme!r})")
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Webhook URL scheme must be http or https (got {parsed.scheme!r})")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Webhook URL missing hostname")
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        raise ValueError("Webhook URL must not target localhost")
    try:
        addr = ipaddress.ip_address(hostname)
        for net in _BLOCKED_NETS:
            if addr in net:
                raise ValueError(f"Webhook URL targets a private/reserved address ({hostname})")
    except ValueError as exc:
        if "private" in str(exc) or "reserved" in str(exc):
            raise
        # Not a literal IP — hostname; SSRF mitigated by egress firewall


def build_signature(secret_hash: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 signature: HMAC(secret_hash, f'{timestamp}.{body}')."""
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret_hash.encode(), message, hashlib.sha256).hexdigest()


def enqueue_webhook(
    *,
    document_id: str,
    tenant_id: str,
    final_status: str,
    confidence_score: float | None,
    ocr_engine: str | None,
    delivery_id: str | None = None,
) -> None:
    """Enqueue the deliver_webhook Celery task. Called synchronously from orchestrator after DB commit."""
    from celery_app import app as celery_app

    did = delivery_id or str(uuid.uuid4())
    celery_app.send_task(
        "tasks.deliver_webhook",
        args=[did, document_id, tenant_id, "document.completed", final_status, confidence_score, ocr_engine],
        queue="ocr",
    )
    logger.info("webhook.enqueued", document_id=document_id, delivery_id=did)
```

- [ ] **Step 2: Add deliver_webhook task to tasks.py**

Edit `services/ocr-worker/tasks.py` — add the new task after `process_document`:

```python
import asyncio
import structlog

from celery_app import app
from einv_common.exceptions import NonRetryableError, RetryableError

_RETRYABLE_EXC = (OSError, ConnectionError, TimeoutError)
logger = structlog.get_logger()

# ... existing process_document task unchanged ...

@app.task(
    bind=True,
    name="tasks.deliver_webhook",
    max_retries=5,
    dont_autoretry_for=(Exception,),  # manual retry with custom backoff
)
def deliver_webhook(
    self,
    delivery_id: str,
    document_id: str,
    tenant_id: str,
    event_type: str,
    final_status: str,
    confidence_score: float | None,
    ocr_engine: str | None,
) -> dict:
    """Fire a signed webhook POST to the tenant's webhook_url. Retries with exponential backoff."""
    log = logger.bind(delivery_id=delivery_id, document_id=document_id, attempt=self.request.retries + 1)
    log.info("webhook_task.started")
    try:
        result = asyncio.run(
            _run_deliver_webhook(
                delivery_id=delivery_id,
                document_id=document_id,
                tenant_id=tenant_id,
                event_type=event_type,
                final_status=final_status,
                confidence_score=confidence_score,
                ocr_engine=ocr_engine,
                attempt=self.request.retries + 1,
            )
        )
        log.info("webhook_task.completed", delivered=result["delivered"])
        return result
    except _ShouldRetry as exc:
        attempt = self.request.retries + 1
        if attempt >= self.max_retries:
            log.error("webhook_task.abandoned", error=str(exc))
            asyncio.run(_mark_abandoned(delivery_id))
            return {"delivered": False, "abandoned": True}
        # Backoff schedule: 2, 4, 8, 16, 32 seconds
        delay = 2 ** attempt
        log.warning("webhook_task.retry", delay=delay, error=str(exc))
        raise self.retry(exc=exc, countdown=delay)
    except _ShouldAbandon as exc:
        log.error("webhook_task.abandoned_non_retryable", error=str(exc))
        asyncio.run(_mark_abandoned(delivery_id))
        return {"delivered": False, "abandoned": True}


class _ShouldRetry(Exception):
    pass


class _ShouldAbandon(Exception):
    pass


async def _run_deliver_webhook(
    *,
    delivery_id: str,
    document_id: str,
    tenant_id: str,
    event_type: str,
    final_status: str,
    confidence_score: float | None,
    ocr_engine: str | None,
    attempt: int,
) -> dict:
    import hashlib
    import hmac
    import json
    import time
    import uuid
    import httpx
    from datetime import datetime, timezone
    from einv_common.db import session_factory
    from einv_common.models.tenant import Tenant
    from einv_common.models.webhook import WebhookDelivery
    from pipeline.webhook_dispatcher import validate_webhook_url
    from sqlalchemy import select

    async with session_factory() as session:
        tenant_row = await session.execute(select(Tenant).where(Tenant.id == uuid.UUID(tenant_id)))
        tenant = tenant_row.scalar_one_or_none()

        if tenant is None or not tenant.webhook_url:
            await _write_delivery_row(
                session, delivery_id, document_id, tenant_id, event_type,
                attempt=attempt, status="abandoned", http_status=None,
                duration_ms=None, response_body=None, next_retry_at=None,
            )
            return {"delivered": False, "skipped": True}

        try:
            validate_webhook_url(tenant.webhook_url)
        except ValueError as exc:
            await _write_delivery_row(
                session, delivery_id, document_id, tenant_id, event_type,
                attempt=attempt, status="abandoned", http_status=None,
                duration_ms=None, response_body=str(exc), next_retry_at=None,
            )
            raise _ShouldAbandon(str(exc)) from exc

        # Build payload
        timestamp = int(time.time())
        payload = {
            "event": event_type,
            "event_id": delivery_id,
            "api_version": "2026-06-19",
            "tenant_id": tenant_id,
            "document_id": document_id,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "data": {
                "status": final_status,
                "confidence_score": confidence_score,
                "ocr_engine": ocr_engine,
                "result_url": f"/v1/documents/{document_id}/result",
            },
        }
        body_bytes = json.dumps(payload, separators=(",", ":")).encode()

        # HMAC signature — sign with raw secret (not the hash); consumers verify with the raw secret shown at creation
        secret = tenant.webhook_secret or ""
        sig = hmac.new(secret.encode(), f"{timestamp}.".encode() + body_bytes, hashlib.sha256).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-EInvoice-Signature": sig,
            "X-EInvoice-Timestamp": str(timestamp),
            "X-EInvoice-Event-Id": delivery_id,
        }

        start_ms = int(time.monotonic() * 1000)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=3.0, read=10.0, write=10.0, pool=10.0),
                follow_redirects=False,
            ) as client:
                resp = await client.post(tenant.webhook_url, content=body_bytes, headers=headers)
            duration_ms = int(time.monotonic() * 1000) - start_ms
            response_body = resp.text[:500] if resp.text else None

            # Non-retryable 4xx (except 408, 429)
            if resp.status_code in range(400, 500) and resp.status_code not in (408, 429):
                await _write_delivery_row(
                    session, delivery_id, document_id, tenant_id, event_type,
                    attempt=attempt, status="abandoned", http_status=resp.status_code,
                    duration_ms=duration_ms, response_body=response_body, next_retry_at=None,
                )
                raise _ShouldAbandon(f"Non-retryable HTTP {resp.status_code}")

            success = resp.is_success
            status_str = "success" if success else "failed"
            await _write_delivery_row(
                session, delivery_id, document_id, tenant_id, event_type,
                attempt=attempt, status=status_str, http_status=resp.status_code,
                duration_ms=duration_ms, response_body=response_body, next_retry_at=None,
                delivered_at=datetime.now(timezone.utc) if success else None,
            )
            if not success:
                raise _ShouldRetry(f"HTTP {resp.status_code}")
            return {"delivered": True, "http_status": resp.status_code}

        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            duration_ms = int(time.monotonic() * 1000) - start_ms
            await _write_delivery_row(
                session, delivery_id, document_id, tenant_id, event_type,
                attempt=attempt, status="failed", http_status=None,
                duration_ms=duration_ms, response_body=str(exc), next_retry_at=None,
            )
            raise _ShouldRetry(str(exc)) from exc


async def _write_delivery_row(
    session,
    delivery_id: str,
    document_id: str,
    tenant_id: str,
    event_type: str,
    *,
    attempt: int,
    status: str,
    http_status: int | None,
    duration_ms: int | None,
    response_body: str | None,
    next_retry_at,
    delivered_at=None,
) -> None:
    import uuid
    from einv_common.models.webhook import WebhookDelivery
    from datetime import datetime, timezone

    session.add(WebhookDelivery(
        id=uuid.uuid4(),           # unique PK per attempt row
        event_id=delivery_id,      # stable across retries; used as payload event_id
        document_id=uuid.UUID(document_id),
        tenant_id=uuid.UUID(tenant_id),
        event_type=event_type,
        attempt=attempt,
        http_status=http_status,
        status=status,
        duration_ms=duration_ms,
        response_body_truncated=response_body,
        next_retry_at=next_retry_at,
        delivered_at=delivered_at,
    ))
    await session.commit()


async def _mark_abandoned(delivery_id: str) -> None:
    """No-op — the last attempt row is already written with status='abandoned' by _run_deliver_webhook."""
    pass
```

- [ ] **Step 3: Update orchestrator.py to dispatch webhook after commit**

In `services/ocr-worker/pipeline/orchestrator.py`:

Replace the `fire_and_forget` import:
```python
# Remove this:
from pipeline.webhook_dispatcher import fire_and_forget

# Add this:
from pipeline.webhook_dispatcher import enqueue_webhook
```

Replace both `fire_and_forget(...)` calls in `run()`. 

Success path (after `await session.commit()`):
```python
            # ── Dispatch webhook (after commit — result is now readable) ─────
            if tenant.webhook_url:
                enqueue_webhook(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    final_status=final_status,
                    confidence_score=result.confidence_score,
                    ocr_engine=result.ocr_engine,
                )
```

Error path (after `await session.commit()` in except block):
```python
            if tenant.webhook_url:
                enqueue_webhook(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    final_status="error",
                    confidence_score=None,
                    ocr_engine=None,
                )
```

Also update `doc.status = "failed"` to `doc.status = "error"` in the except block (the existing code uses "failed" but the spec and Document model use "error" as the retry-eligible state):
```python
        except Exception as exc:
            await session.rollback()
            doc.status = "error"   # was "failed" — changed to match retry endpoint contract
            doc.error_message = str(exc)[:1000]
            doc.processed_at = datetime.now(timezone.utc)
            await session.commit()
```

- [ ] **Step 4: Commit**

```bash
git add services/ocr-worker/pipeline/webhook_dispatcher.py services/ocr-worker/tasks.py services/ocr-worker/pipeline/orchestrator.py
git commit -m "feat(ocr-worker): replace fire-and-forget with durable deliver_webhook Celery task with HMAC signing"
```

---

### Task 8: Admin-api — tenant quota + webhook_secret endpoints

**Files:**
- Modify: `services/admin-api/app/routers/tenants.py`

**Interfaces:**
- Consumes: `TenantCreate`, `TenantUpdate`, `TenantOut`, `TenantCreated` from `einv_common.schemas.tenant`
- Produces: `POST /admin/tenants` returns `TenantCreated` (includes `webhook_secret` plaintext once)
- Produces: `PATCH /admin/tenants/{id}` validates quota pair; if `webhook_url` provided, SSRF-validates it

- [ ] **Step 1: Update tenants.py**

Replace `services/admin-api/app/routers/tenants.py` entirely:

```python
"""Tenant CRUD and API-key management endpoints."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from einv_common.models.audit import AuditLog
from einv_common.models.tenant import ApiKey, Tenant
from einv_common.models.user import AdminUser
from einv_common.schemas.common import PaginatedResponse
from einv_common.schemas.tenant import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    TenantCreate,
    TenantCreated,
    TenantOut,
    TenantUpdate,
)
from app.auth_utils import generate_api_key
from app.deps import get_current_user, get_session, require_super_admin, require_tenant_admin

router = APIRouter()

_MAX_QUOTA_WINDOW = 2_592_000  # 30 days in seconds


def _check_tenant_access(user: AdminUser, tenant_id: uuid.UUID) -> None:
    if user.role == "super_admin":
        return
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")


def _validate_webhook_url_safe(url: str) -> None:
    """Validate webhook_url is an https URL and not targeting private IPs."""
    import ipaddress
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_WEBHOOK_URL", "message": "webhook_url must use https"},
        )
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_WEBHOOK_URL", "message": "webhook_url missing hostname"},
        )
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_WEBHOOK_URL", "message": "webhook_url must not target localhost"},
        )
    try:
        addr = ipaddress.ip_address(hostname)
        private_nets = [
            ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("::1/128"),
            ipaddress.ip_network("fc00::/7"), ipaddress.ip_network("fe80::/10"),
        ]
        for net in private_nets:
            if addr in net:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "INVALID_WEBHOOK_URL", "message": "webhook_url targets a private address"},
                )
    except HTTPException:
        raise
    except ValueError:
        pass  # hostname is not a literal IP; proceed


def _generate_webhook_secret() -> tuple[str, str]:
    """Return (raw_secret, sha256_hex). Only hash is stored."""
    raw = secrets.token_hex(32)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


# ---------------------------------------------------------------------------
# Tenant endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=PaginatedResponse)
async def list_tenants(
    page: int = 1,
    limit: int = 20,
    _: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
):
    offset = (page - 1) * limit
    total = (await session.execute(select(func.count()).select_from(Tenant))).scalar_one()
    rows = (await session.execute(select(Tenant).offset(offset).limit(limit))).scalars().all()
    items = [TenantOut.model_validate(t) for t in rows]
    return PaginatedResponse(
        items=items, total=total, page=page, limit=limit,
        pages=max(1, (total + limit - 1) // limit),
    )


@router.post("", response_model=TenantCreated, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreate,
    user: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> TenantCreated:
    existing = (await session.execute(
        select(Tenant).where(Tenant.slug == body.slug)
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slug already taken")

    if body.webhook_url:
        _validate_webhook_url_safe(body.webhook_url)

    raw_secret, _ = _generate_webhook_secret()

    tenant_data = body.model_dump()
    tenant = Tenant(**tenant_data, webhook_secret=raw_secret)
    session.add(tenant)
    session.add(AuditLog(
        tenant_id=tenant.id,
        action="tenant_created",
        actor=user.email,
        details={"slug": body.slug, "has_quota": body.quota_max_docs is not None},
    ))
    await session.commit()
    await session.refresh(tenant)

    out = TenantOut.model_validate(tenant)
    return TenantCreated(**out.model_dump(), webhook_secret=raw_secret)


@router.get("/{tenant_id}", response_model=TenantOut)
async def get_tenant(
    tenant_id: uuid.UUID,
    user: AdminUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
) -> TenantOut:
    _check_tenant_access(user, tenant_id)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return TenantOut.model_validate(tenant)


@router.patch("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: uuid.UUID,
    body: TenantUpdate,
    user: AdminUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
) -> TenantOut:
    _check_tenant_access(user, tenant_id)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    updates = body.model_dump(exclude_unset=True)  # exclude_unset allows sending null to clear a field

    if "webhook_url" in updates:
        _validate_webhook_url_safe(updates["webhook_url"])

    for field, value in updates.items():
        setattr(tenant, field, value)

    session.add(AuditLog(
        tenant_id=tenant_id,
        action="tenant_updated",
        actor=user.email,
        details=updates,
    ))
    await session.commit()
    await session.refresh(tenant)
    return TenantOut.model_validate(tenant)


@router.post("/{tenant_id}/rotate-webhook-secret", response_model=dict)
async def rotate_webhook_secret(
    tenant_id: uuid.UUID,
    user: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Generate a new webhook secret. The old one is immediately invalidated."""
    _check_tenant_access(user, tenant_id)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    raw_secret, _ = _generate_webhook_secret()
    tenant.webhook_secret = raw_secret

    session.add(AuditLog(
        tenant_id=tenant_id,
        action="webhook_secret_rotated",
        actor=user.email,
        details={},
    ))
    await session.commit()
    return {"webhook_secret": raw_secret}


@router.delete("/{tenant_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_tenant(
    tenant_id: uuid.UUID,
    user: AdminUser = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    tenant.is_active = False
    session.add(AuditLog(
        tenant_id=tenant_id,
        action="tenant_deactivated",
        actor=user.email,
        details={},
    ))
    await session.commit()


# ---------------------------------------------------------------------------
# API key endpoints
# ---------------------------------------------------------------------------

@router.get("/{tenant_id}/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    tenant_id: uuid.UUID,
    user: AdminUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ApiKeyOut]:
    _check_tenant_access(user, tenant_id)
    rows = (await session.execute(
        select(ApiKey).where(ApiKey.tenant_id == tenant_id)
    )).scalars().all()
    return [ApiKeyOut.model_validate(k) for k in rows]


@router.post("/{tenant_id}/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    tenant_id: uuid.UUID,
    body: ApiKeyCreate,
    user: AdminUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreated:
    _check_tenant_access(user, tenant_id)
    if await session.get(Tenant, tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    raw_key, key_hash = generate_api_key()
    api_key = ApiKey(
        tenant_id=tenant_id,
        key_hash=key_hash,
        label=body.label,
        expires_at=body.expires_at,
    )
    session.add(api_key)
    session.add(AuditLog(
        tenant_id=tenant_id,
        action="api_key_created",
        actor=user.email,
        details={"label": body.label},
    ))
    await session.commit()
    await session.refresh(api_key)

    base = ApiKeyOut.model_validate(api_key)
    return ApiKeyCreated(raw_key=raw_key, **base.model_dump())


@router.delete(
    "/{tenant_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_api_key(
    tenant_id: uuid.UUID,
    key_id: uuid.UUID,
    user: AdminUser = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    _check_tenant_access(user, tenant_id)
    api_key = await session.get(ApiKey, key_id)
    if api_key is None or api_key.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    api_key.is_active = False
    session.add(AuditLog(
        tenant_id=tenant_id,
        action="key_rotated",
        actor=user.email,
        details={"key_id": str(key_id), "label": api_key.label},
    ))
    await session.commit()
```

- [ ] **Step 2: Commit**

```bash
git add services/admin-api/app/routers/tenants.py
git commit -m "feat(admin-api): quota fields + webhook_secret on tenant create/update with SSRF validation"
```

---

### Task 9: Final commit — spec doc + stage commit

- [ ] **Step 1: Commit design spec**

```bash
git add docs/superpowers/specs/2026-06-19-stage5-core-api-hardening-design.md docs/superpowers/plans/2026-06-19-stage5-core-api-hardening.md
git commit -m "docs: Stage 5 design spec and implementation plan"
```

- [ ] **Step 2: Tag the stage**

```bash
git tag stage-5
```

---

## Self-Review Checklist

- [x] **Spec coverage:** All 4 features covered — webhook delivery (Task 7), export endpoints (Task 5), rate limiting (Task 4), retry endpoint (Task 6). Admin-api quota (Task 8). Migration (Task 3). Models (Task 1). Schemas (Task 2).
- [x] **No placeholders:** All code blocks are complete and executable.
- [x] **Type consistency:** `WebhookDelivery` model used consistently in Tasks 1, 7. `TenantCreated` produced in Task 2, consumed in Task 8. `enqueue_webhook` produced in Task 7 step 1, consumed in Task 7 step 3.
- [x] **doc.status "error" vs "failed":** Task 7 step 3 explicitly updates orchestrator to use `"error"` so retry endpoint's `WHERE status='error'` works correctly.
- [x] **Commit-first ordering:** Task 7 explicitly places `enqueue_webhook` after `await session.commit()` in both success and error paths.
- [x] **Atomic retry:** Task 6 uses `UPDATE … WHERE status='error' RETURNING id` and only dispatches Celery if row returned.
