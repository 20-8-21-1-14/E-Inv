# Stage 5: Core-API Production Hardening

**Date:** 2026-06-19  
**Status:** Implemented  
**Branch:** `feature/stage-5-hardening`  
**Services affected:** `common`, `core-api`, `ocr-worker`, `admin-api`, `migrations`

---

## Goal

Bridge the platform from "built" to "usable end-to-end": a tenant uploads a VAT invoice, it processes, a signed webhook fires to their system, they can pull the result or download it as CSV/JSON, and the platform enforces per-tenant upload quotas.

---

## Architecture

No new microservices or Docker containers. All changes within existing services.

```
┌─────────────┐     upload      ┌──────────────┐
│   Tenant    │ ──────────────► │   core-api   │  ← rate limit check (Redis Lua)
│   Client    │                 └──────┬───────┘
└─────────────┘                        │ queue OCR task
                                       ▼
                                ┌──────────────┐
                                │  ocr-worker  │  process_document
                                └──────┬───────┘
                                       │ commit result → then dispatch deliver_webhook
                                       ▼
                                ┌──────────────┐
                                │  ocr-worker  │  deliver_webhook (Celery retry)
                                └──────┬───────┘
                                       │ write webhook_deliveries row per attempt
                                       ▼
                                  PostgreSQL + tenant.webhook_url

core-api new endpoints:
  GET  /v1/documents/{id}/export/csv
  GET  /v1/documents/{id}/export/json
  POST /v1/documents/{id}/retry
```

| Service | Changes |
|---|---|
| `common` | `WebhookDelivery` model; `quota_max_docs`, `quota_window_seconds`, `webhook_secret` on `Tenant`; `processing_attempts`, `last_retry_at` on `Document` |
| `core-api` | Redis Lua rate limiter, export router, retry endpoint |
| `ocr-worker` | Dispatch `deliver_webhook` after committing result; `doc.status = "error"` (was "failed") |
| `admin-api` | Expose quota + webhook_secret fields; `POST /{id}/rotate-webhook-secret` |
| `migrations` | 1 Alembic migration: `0002_stage5_hardening` |

---

## Global Constraints

- Python 3.12+, FastAPI 0.110+, SQLAlchemy 2.x async `mapped_column` style
- All monetary values serialized as strings (decimal notation) — never floats
- Webhook secret stored as raw hex in DB (it's a signing key, not a password — hashing would break HMAC signing); shown once at creation
- Rate limit keys must not be evicted — Redis `noeviction` policy recommended
- Fail-open on Redis unavailability (log WARNING, allow upload)
- Max 3 tenant-triggered retries per document (`processing_attempts >= 3` → 429)
- Webhook: max 5 Celery attempts, exponential backoff 2→4→8→16→32s, abandon on non-transient 4xx
- No new Docker services or containers

---

## Feature 1: Webhook Delivery

### Trigger

After `tasks.process_document` finishes (success **or** error), the worker:
1. Commits the result/status to PostgreSQL
2. **Then** enqueues `tasks.deliver_webhook` — never before the commit

### Celery Task: `deliver_webhook`

- Fires HTTP POST to `tenant.webhook_url`
- Connect timeout: 3s, read timeout: 10s, no redirect following
- Max 5 attempts, exponential backoff + jitter: 2s → 4s → 8s → 16s → 32s
- **Retry on:** network errors, 408, 429, 5xx
- **Abandon immediately on:** 4xx (except 408, 429)
- After 5 failures → final `webhook_deliveries.status = 'abandoned'`

### Payload

```json
{
  "event": "document.completed",
  "event_id": "<delivery_id (stable across retries)>",
  "api_version": "2026-06-19",
  "tenant_id": "...",
  "document_id": "...",
  "occurred_at": "<ISO 8601>",
  "data": {
    "status": "done|hitl|error",
    "confidence_score": 0.97,
    "ocr_engine": "paddleocr",
    "result_url": "/v1/documents/{id}/result"
  }
}
```

### Security

- Per-tenant `webhook_secret` — random 32-byte hex, stored raw in DB, shown once at creation
- Every request: `X-EInvoice-Signature: HMAC-SHA256("{timestamp}.{body}")`, `X-EInvoice-Timestamp`, `X-EInvoice-Event-Id`
- SSRF protection: HTTPS only, block private/loopback/link-local/metadata IP ranges, no redirects
- Validated at admin-api on save and re-validated at delivery time

### `webhook_deliveries` Table

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | unique per attempt row |
| `event_id` | VARCHAR(36) | stable across retries; used as payload `event_id` |
| `document_id` | UUID FK → documents | CASCADE |
| `tenant_id` | UUID FK → tenants | CASCADE |
| `event_type` | VARCHAR(50) | `document.completed` |
| `attempt` | INTEGER | CHECK > 0 |
| `http_status` | INTEGER NULL | null if network error; CHECK 100–599 |
| `status` | VARCHAR(20) | `pending / success / failed / abandoned` |
| `duration_ms` | INTEGER NULL | |
| `response_body_truncated` | TEXT NULL | first 500 chars |
| `next_retry_at` | TIMESTAMPTZ NULL | |
| `delivered_at` | TIMESTAMPTZ NULL | set on success |
| `created_at` | TIMESTAMPTZ | server default `now()` |

Indexes: `(tenant_id, document_id)`, `(document_id, attempt)`, `(status, next_retry_at)`

---

## Feature 2: Export Endpoints

Both require tenant API key auth. Return `404` if status is not `done` or `hitl`.

### `GET /v1/documents/{id}/export/csv`

- One row per line item; invoice header columns repeated on each row
- Columns: `invoice_number, invoice_date, seller_tax_id, buyer_tax_id, seller_name, buyer_name, grand_total, currency, line_number, item_name, item_code, unit, quantity, unit_price, tax_rate, tax_amount, total_amount`
- CSV injection protection: prefix `=`, `+`, `-`, `@` with `'`
- Fallback row with empty line-item columns if no line items exist
- Headers: `Content-Type: text/csv`, `Content-Disposition: attachment; filename="{document_id}.csv"`

### `GET /v1/documents/{id}/export/json`

```json
{
  "schema_version": "1.0",
  "document_id": "...",
  "doc_type": "vat_invoice",
  "extracted_at": "<ISO 8601>",
  "header": { "invoice_number": "...", "invoice_date": "YYYY-MM-DD", "seller_tax_id": "...", "seller_name": "...", "buyer_tax_id": "...", "buyer_name": "..." },
  "totals": { "subtotal": "1000000.00", "total_tax": "100000.00", "grand_total": "1100000.00", "currency": "VND" },
  "line_items": [{ "line_number": 1, "item_name": "...", "quantity": "2.0000", "unit_price": "500000.00", "tax_rate": "10.00", "tax_amount": "100000.00", "total_amount": "1100000.00" }],
  "confidence": { "document_score": 0.97, "fields": { "invoice_number": 0.99 } }
}
```

All monetary values are strings to avoid float precision loss.

---

## Feature 3: Rate Limiting

### Tenant Config

| Field | Type | Constraint |
|---|---|---|
| `quota_max_docs` | INTEGER NULL | > 0 |
| `quota_window_seconds` | INTEGER NULL | 1 – 2,592,000 (30 days) |

Both null = unlimited. One set, one null = admin-api rejects. Window changes apply prospectively.

### Atomic Redis Lua (sliding window)

```lua
-- KEYS[1] = "ratelimit:{tenant_id}"
-- ARGV[1] = quota_max_docs, ARGV[2] = window_ms, ARGV[3] = unique event UUID
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
```

- `now` from Redis `TIME` — immune to app server clock skew
- `remaining == -1` → HTTP 429 with `Retry-After` header
- Redis down → **fail-open** (log `WARNING ratelimit.redis_unavailable`)
- Invalid quota config (`<= 0`) → fail-open with `WARNING ratelimit.invalid_quota_config`

---

## Feature 4: Document Retry

### `POST /v1/documents/{id}/retry`

| Response | Condition |
|---|---|
| 404 | Document not found / not owned by tenant |
| 409 | Document not in `error` state |
| 429 | `processing_attempts >= 3` (checked before UPDATE) |
| 202 | Accepted, re-queued |

Atomic SQL pattern:
```sql
UPDATE documents
SET status = 'queued', error_message = NULL,
    processing_attempts = processing_attempts + 1, last_retry_at = now()
WHERE id = :id AND tenant_id = :tenant_id
  AND status = 'error' AND processing_attempts < 3
RETURNING id, processing_attempts;
```

Only enqueues `tasks.process_document` if UPDATE returns a row (TOCTOU-safe).  
Writes `AuditLog(action="retry", actor="api_key", details={"attempt": n})`.

### New Document Columns

| Column | Type | Notes |
|---|---|---|
| `processing_attempts` | INTEGER NOT NULL DEFAULT 0 | |
| `last_retry_at` | TIMESTAMPTZ NULL | |

---

## Database Migration: `0002_stage5_hardening`

File: `migrations/versions/0002_stage5_hardening.py`

- ALTER TABLE `tenants` ADD: `webhook_secret VARCHAR(64)`, `quota_max_docs INTEGER`, `quota_window_seconds INTEGER`
- ALTER TABLE `documents` ADD: `processing_attempts INTEGER NOT NULL DEFAULT 0`, `last_retry_at TIMESTAMPTZ`
- CREATE TABLE `webhook_deliveries` (full schema above) with 3 indexes and 2 CHECK constraints

---

## Out of Scope (Stage 5)

- Transactional outbox pattern for webhook delivery (defer to Stage 7)
- API key scopes (`exports:read`, `documents:write`)
- Dedicated Redis deployment for rate limit keys
- Full observability/alerting (Prometheus rules, Grafana dashboards)
- Multi-doc-type support (BOL, packing list, shipping instruction)

---

## Implementation Checklist

### Task 1: Common model changes
- [x] Add `webhook_secret`, `quota_max_docs`, `quota_window_seconds` to `Tenant` model
- [x] Add `processing_attempts`, `last_retry_at` to `Document` model
- [x] Create `common/einv_common/models/webhook.py` — `WebhookDelivery` ORM model
- [x] Export `WebhookDelivery` from `common/einv_common/models/__init__.py`

### Task 2: Common schemas
- [x] Update `common/einv_common/schemas/tenant.py` — `TenantCreate` + `TenantUpdate` with quota fields + `@model_validator`, `TenantCreated(TenantOut)` with `webhook_secret: str`

### Task 3: Alembic migration
- [x] Create `migrations/versions/0002_stage5_hardening.py`

### Task 4: Rate limiter (core-api)
- [x] Create `services/core-api/app/ratelimit.py` — atomic Lua sliding window
- [x] Wire `check_quota(tenant, redis)` into upload endpoint after MIME/size validation

### Task 5: Export endpoints (core-api)
- [x] Create `services/core-api/app/routers/exports.py` — CSV + JSON
- [x] Register exports router in `services/core-api/app/main.py`

### Task 6: Retry endpoint (core-api)
- [x] Add `POST /v1/documents/{id}/retry` to `services/core-api/app/routers/documents.py`

### Task 7: Webhook delivery (ocr-worker)
- [x] Rewrite `services/ocr-worker/pipeline/webhook_dispatcher.py` — `enqueue_webhook` + `validate_webhook_url`
- [x] Add `deliver_webhook` Celery task to `services/ocr-worker/tasks.py`
- [x] Update `services/ocr-worker/pipeline/orchestrator.py` — use `enqueue_webhook` after commit; `doc.status = "error"` (was "failed")

### Task 8: Admin-api tenant endpoints
- [x] Update `services/admin-api/app/routers/tenants.py` — `TenantCreated` on POST, `exclude_unset` on PATCH, SSRF validation, `POST /{id}/rotate-webhook-secret`
