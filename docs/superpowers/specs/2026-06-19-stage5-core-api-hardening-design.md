# Stage 5: Core-API Production Hardening

**Date:** 2026-06-19
**Status:** Approved
**Services affected:** `common`, `core-api`, `ocr-worker`, `admin-api`
**Migration:** 1 Alembic migration

---

## Goal

Bridge the platform from "built" to "usable end-to-end": a tenant uploads a VAT invoice, it processes, a signed webhook fires to their system, they can pull the result or download it as CSV/JSON, and the platform enforces per-tenant upload quotas.

---

## Architecture Overview

No new microservices or Docker containers. All changes are within existing services.

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
| `ocr-worker` | Dispatch `deliver_webhook` after committing result |
| `admin-api` | Expose quota + webhook_secret fields on tenant create/update |
| `migrations` | 1 Alembic migration |

---

## Feature 1: Webhook Delivery

### Trigger

After `tasks.process_document` finishes (success **or** error), the worker:
1. Commits the result/status to PostgreSQL
2. **Then** enqueues `tasks.deliver_webhook` — never before the commit

This ordering prevents the delivery task from reading stale data.

### Celery Task: `deliver_webhook`

- Fires HTTP POST to `tenant.webhook_url`
- Connect timeout: 3s, read timeout: 10s
- No redirect following
- Max 5 attempts with exponential backoff + jitter: 2s → 4s → 8s → 16s → 32s
- **Retry on:** network errors, 408, 429, 5xx
- **Abandon immediately on:** 4xx (except 408, 429) — misconfigured endpoint, not transient
- After 5 failures → final `webhook_deliveries.status = 'abandoned'`

### Payload Contract (versioned)

```json
{
  "event": "document.completed",
  "event_id": "<webhook_delivery.id>",
  "api_version": "2026-06-19",
  "tenant_id": "...",
  "document_id": "...",
  "occurred_at": "<ISO 8601>",
  "data": {
    "status": "done",
    "confidence_score": 0.97,
    "ocr_engine": "paddleocr",
    "result_url": "/v1/documents/{id}/result"
  }
}
```

Events fired: `document.completed` (status=done/hitl/error).

### Security

- Per-tenant `webhook_secret` (random 32-byte hex, stored hashed in DB, shown once at creation)
- Every request includes:
  - `X-EInvoice-Signature: HMAC-SHA256("{timestamp}.{body}")`
  - `X-EInvoice-Timestamp: <unix epoch>`
  - `X-EInvoice-Event-Id: <delivery_id>`
- Consumers validate signature and reject if timestamp is > 5 minutes old
- SSRF protection on `webhook_url`:
  - HTTPS only in production
  - Block private, loopback, link-local, and metadata IP ranges after DNS resolution
  - No redirect following
  - Validated at admin-api on save and re-validated at delivery time

### `webhook_deliveries` Table

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | also used as `event_id` in payload |
| `document_id` | UUID FK → documents | |
| `tenant_id` | UUID FK → tenants | |
| `event_type` | VARCHAR(50) | e.g. `document.completed` |
| `attempt` | INTEGER | 1-indexed |
| `http_status` | INTEGER NULL | null if network error |
| `status` | VARCHAR(20) | `pending / success / failed / abandoned` |
| `duration_ms` | INTEGER NULL | |
| `response_body_truncated` | TEXT NULL | first 500 chars |
| `next_retry_at` | TIMESTAMPTZ NULL | |
| `delivered_at` | TIMESTAMPTZ NULL | set on success |
| `created_at` | TIMESTAMPTZ | server default now() |

Indexes: `(tenant_id, document_id)`, `(document_id, attempt)`, `(status, next_retry_at)`

---

## Feature 2: Export Endpoints

Both endpoints require tenant API key auth and return `404` if document status is not `done` or `hitl`.

### `GET /v1/documents/{id}/export/csv`

- **One row per line item**, invoice header columns repeated on each row
- Columns: `invoice_number, invoice_date, seller_tax_id, buyer_tax_id, seller_name, buyer_name, grand_total, currency, line_number, item_name, item_code, unit, quantity, unit_price, tax_rate, tax_amount, total_amount`
- CSV injection protection: prefix cell values starting with `=`, `+`, `-`, `@` with `'`
- Headers: `Content-Type: text/csv`, `Content-Disposition: attachment; filename="{document_id}.csv"`

### `GET /v1/documents/{id}/export/json`

Stable versioned schema — not a raw internal model dump:

```json
{
  "schema_version": "1.0",
  "document_id": "...",
  "doc_type": "vat_invoice",
  "extracted_at": "<ISO 8601>",
  "header": {
    "invoice_number": "...",
    "invoice_date": "YYYY-MM-DD",
    "seller_tax_id": "...",
    "seller_name": "...",
    "buyer_tax_id": "...",
    "buyer_name": "..."
  },
  "totals": {
    "subtotal": "1000000.00",
    "total_tax": "100000.00",
    "grand_total": "1100000.00",
    "currency": "VND"
  },
  "line_items": [
    {
      "line_number": 1,
      "item_name": "...",
      "item_code": "...",
      "unit": "...",
      "quantity": "2.0000",
      "unit_price": "500000.00",
      "tax_rate": "10.00",
      "tax_amount": "100000.00",
      "total_amount": "1100000.00"
    }
  ],
  "confidence": {
    "document_score": 0.97,
    "fields": {
      "invoice_number": 0.99,
      "grand_total": 0.95
    }
  }
}
```

All monetary values are strings (decimal notation) to avoid float precision loss. Dates are `YYYY-MM-DD`.

---

## Feature 3: Rate Limiting

### Tenant Model Changes

Two new nullable columns on `tenants`:
- `quota_max_docs INTEGER NULL`
- `quota_window_seconds INTEGER NULL`

**Semantics:**
- Both null → unlimited (default for existing tenants)
- Both set → enforced
- One set, one null → admin-api rejects as config error

**Validation in admin-api:**
- `quota_max_docs > 0`
- `0 < quota_window_seconds <= 2_592_000` (30 days max)
- Window changes apply prospectively — Redis only holds recent history

### Enforcement: Redis Lua Script (atomic)

```lua
-- Called at upload time, before file storage
-- KEYS[1] = "ratelimit:{tenant_id}"
-- ARGV[1] = quota_max_docs
-- ARGV[2] = quota_window_ms (milliseconds)
-- ARGV[3] = ratelimit_event_id (unique UUID per upload attempt)

local t = redis.call('TIME')  -- single call to avoid skew between two calls
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

- `now` is derived from Redis `TIME` — immune to app server clock skew
- Member is a unique `ratelimit_event_id` (UUID) — not `document_id`, avoids slot collision on retry
- Returns `{remaining, retry_after_seconds}`; `remaining == -1` means rejected
- On rejection: HTTP 429 with `Retry-After: <seconds>` header

**Redis availability:**
- If Redis is unreachable → **fail open** (allow upload, log `WARNING ratelimit.redis_unavailable`)
- Fail-open is a deliberate tradeoff: Redis outage must not block all ingestion
- Emit metric `ratelimit.fail_open_count` for alerting; reconcile from PostgreSQL audit_logs

**Redis configuration note:** Rate limit keys must not be evicted mid-window. Set `maxmemory-policy noeviction` or isolate rate limit keys from cache workloads. Monitor `evicted_keys` metric.

---

## Feature 4: Document Retry

### `POST /v1/documents/{id}/retry`

**Atomic state transition (prevents duplicate Celery tasks):**
```sql
UPDATE documents
SET status = 'queued',
    error_message = NULL,
    processing_attempts = processing_attempts + 1,
    last_retry_at = now()
WHERE id = :id
  AND tenant_id = :tenant_id
  AND status = 'error'
RETURNING id;
```

Only enqueues `tasks.process_document` if the UPDATE returns a row.

**Limits and responses:**
- `404` — document not found or not owned by tenant
- `409` — document is not in `error` state
- `429` — `processing_attempts >= 3` checked **before** the UPDATE (max retries reached)
- `202` — accepted, re-queued

**Audit:** writes `AuditLog(action="retry", actor="api_key", details={"attempt": n})`

### `Document` Model Changes

Two new columns:
- `processing_attempts INTEGER NOT NULL DEFAULT 0`
- `last_retry_at TIMESTAMPTZ NULL`

---

## Database Migration

Single Alembic migration (`stage5_hardening`):

```
ALTER TABLE tenants
  ADD COLUMN quota_max_docs INTEGER NULL,
  ADD COLUMN quota_window_seconds INTEGER NULL,
  ADD COLUMN webhook_secret_hash VARCHAR(128) NULL;

ALTER TABLE documents
  ADD COLUMN processing_attempts INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN last_retry_at TIMESTAMPTZ NULL;

CREATE TABLE webhook_deliveries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  event_type VARCHAR(50) NOT NULL,
  attempt INTEGER NOT NULL CHECK (attempt > 0),
  http_status INTEGER NULL CHECK (http_status BETWEEN 100 AND 599),
  status VARCHAR(20) NOT NULL DEFAULT 'pending',
  duration_ms INTEGER NULL,
  response_body_truncated TEXT NULL,
  next_retry_at TIMESTAMPTZ NULL,
  delivered_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_webhook_deliveries_tenant_doc ON webhook_deliveries(tenant_id, document_id);
CREATE INDEX ix_webhook_deliveries_doc_attempt ON webhook_deliveries(document_id, attempt);
CREATE INDEX ix_webhook_deliveries_status_retry ON webhook_deliveries(status, next_retry_at);
```

---

## Out of Scope (Stage 5)

- Transactional outbox pattern for webhook delivery (defer to Stage 7)
- API key scopes (`exports:read`, `documents:write`)
- Dedicated Redis deployment for rate limit keys
- Full observability/alerting setup (Prometheus rules, Grafana dashboards)
- Multi-doc-type support (BOL, packing list, shipping instruction)
