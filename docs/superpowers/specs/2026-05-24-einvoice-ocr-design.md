# e-Invoice Hybrid OCR System — Design Spec
**Date:** 2026-05-24  
**Status:** Approved  
**Scope:** v1 — Vietnamese VAT Invoice (XML + PDF/image); v2+ — full logistics suite

---

## 1. Problem & Goals

Automate extraction of structured data from Vietnamese logistics invoices and documents. Eliminate manual data entry, enforce validation rules, and expose clean JSON via REST API to downstream ERP systems.

**Primary accuracy strategy:** XML-first bypass — Vietnamese e-invoices mandated by Nghị định 123/2020 and Thông tư 32/2025 carry a signed XML file. Parsing XML directly yields 100% accuracy at zero compute cost. OCR is only invoked for PDF scans and images.

**Key non-goals (v1):** ERP push integration, mobile UI, BOL/Packing List processing (v2+).

---

## 2. Constraints

| Dimension | Decision |
|---|---|
| Deployment | Private VPS, Docker Compose, self-managed |
| Scale | <500 docs/day launch → 500–5k/day target |
| OCR engine | Self-built PaddleOCR (fine-tuned) + Claude Vision fallback |
| Third-party APIs | None — fully self-hosted |
| Tenancy | Multi-tenant, row-level isolation, per-tenant config |
| Interface | REST API + Admin panel (HITL + flow control) |
| Language | Python 3.11+, async/coroutines throughout |
| Compliance | Audit log, 10-year document retention (Vietnamese law) |

---

## 3. Repository Structure

```
e-invoice/
├── services/
│   ├── core-api/                   # FastAPI REST API — ingestion, auth, results
│   │   ├── app/
│   │   │   ├── routers/            # documents, results, webhooks
│   │   │   ├── middleware/         # auth, rate-limiting, tenant context
│   │   │   └── main.py
│   │   └── Dockerfile
│   ├── ocr-worker/                 # Celery — OCR pipeline engine
│   │   ├── pipeline/
│   │   │   ├── preprocessor.py     # image quality correction
│   │   │   ├── detector.py         # format detection (XML vs PDF vs image)
│   │   │   ├── xml_parser.py       # XML bypass (GDT schema 1.0.7)
│   │   │   ├── ocr_engine.py       # PaddleOCR async wrapper
│   │   │   ├── llm_fallback.py     # Claude Vision API async client
│   │   │   ├── extractor.py        # structured field extraction per doc type
│   │   │   ├── validator.py        # business rules + math checks
│   │   │   └── scorer.py           # per-field confidence scoring
│   │   ├── tasks.py                # Celery task definitions
│   │   └── Dockerfile
│   └── admin-api/                  # FastAPI — HITL queue, flow control, config
│       ├── app/
│       │   ├── routers/            # hitl, tenants, system, users
│       │   └── main.py
│       └── Dockerfile
├── common/                         # pip-installable shared package
│   ├── models/                     # SQLAlchemy 2.0 ORM models
│   ├── schemas/                    # Pydantic v2 schemas
│   ├── db/                         # async session factory (asyncpg)
│   ├── storage/                    # MinIO async client wrapper
│   └── config.py                   # Pydantic Settings (env-based)
├── training/                       # ML lifecycle — fully separate from runtime
│   ├── data/
│   │   ├── raw/                    # collected invoice scans (anonymised)
│   │   ├── annotated/              # PPOCRLabel format annotations
│   │   ├── synthetic/              # generator output
│   │   └── splits/                 # train/ val/ test/ (70/15/15)
│   ├── annotation/
│   │   ├── label_schema.json       # canonical field names per doc type
│   │   └── annotation_guide.md
│   ├── synthetic/
│   │   ├── generator.py            # renders invoice images from templates
│   │   ├── text_corpus.py          # Vietnamese business text corpus
│   │   ├── noise_augment.py        # degradation simulation
│   │   └── fonts/
│   │       └── templates/
│   ├── preprocessing/
│   │   ├── split.py                # stratified split
│   │   ├── augmentation.py         # training-time augmentation
│   │   └── convert_labels.py       # PPOCRLabel → PaddleOCR format
│   ├── configs/
│   │   ├── det_ppocr_v4.yml        # DBNet detection config
│   │   ├── rec_ppocr_v4.yml        # SVTRv2 recognition config
│   │   ├── table_slanet.yml        # SLANet table structure config
│   │   └── layout_ppstructure.yml  # PP-Structure layout config
│   ├── scripts/
│   │   ├── train_det.sh
│   │   ├── train_rec.sh
│   │   ├── train_table.sh
│   │   ├── train_all.sh
│   │   ├── evaluate.py             # CER / Hmean / TEDS metrics
│   │   ├── export_model.py         # → Paddle Inference static model
│   │   ├── export_onnx.py          # optional ONNX export
│   │   └── push_model.py           # version + upload to MinIO registry
│   ├── feedback/
│   │   ├── export_corrections.py   # HITL corrections → PPOCRLabel format
│   │   └── merge_dataset.py        # merge corrections into dataset
│   ├── notebooks/
│   │   ├── 01_data_exploration.ipynb
│   │   ├── 02_model_evaluation.ipynb
│   │   └── 03_ablation.ipynb
│   └── requirements.txt            # heavy training deps (not in Docker images)
├── docs/
│   └── superpowers/specs/
├── plan/
├── docker-compose.yml
├── docker-compose.override.yml     # local dev overrides
└── requirements/
    ├── base.txt                    # shared: sqlalchemy, pydantic, httpx, structlog
    ├── core-api.txt
    ├── ocr-worker.txt              # heavy: paddlepaddle, paddleocr, opencv
    ├── admin-api.txt
    └── training.txt                # training-only: paddlepaddle, text-renderer, jupyter
```

---

## 4. Data Model

### Multi-tenancy strategy
Row-level isolation — every tenant-scoped table carries `tenant_id UUID NOT NULL`. Application middleware extracts the tenant from the API key on each request and injects it into a request-scoped context. Every ORM query filters by `tenant_id` at the SQLAlchemy layer. Simpler than PostgreSQL RLS with async connection pooling; sufficient for VPS scale.

### PostgreSQL schema

```sql
-- Root tenant record
tenants (
  id UUID PK, name, slug UNIQUE,
  confidence_threshold FLOAT DEFAULT 0.95,
  webhook_url VARCHAR NULL,
  validation_rules JSONB,         -- per-tenant custom rules
  is_active BOOL, created_at
)

-- API key rotation support
api_keys (
  id UUID PK, tenant_id FK,
  key_hash VARCHAR UNIQUE,        -- SHA-256 of raw key
  label VARCHAR, is_active BOOL,
  created_at, last_used_at, expires_at
)

-- One row per uploaded file
documents (
  id UUID PK, tenant_id FK,
  doc_type   VARCHAR,             -- vat_invoice | freight_invoice | bol | packing_list | pod
  source_format VARCHAR,          -- xml | pdf | image
  status     VARCHAR,             -- queued | processing | done | hitl | error
  file_path  VARCHAR,             -- MinIO object path
  file_hash  VARCHAR,             -- SHA-256 for dedup
  task_id    VARCHAR,             -- Celery task ID
  created_at, processed_at, error_message
)

-- One row per processed document
extraction_results (
  id UUID PK, document_id FK, tenant_id FK,
  confidence_score FLOAT,         -- average across all fields
  raw_fields       JSONB,         -- raw OCR output
  validated_fields JSONB,         -- post-validation clean output
  validation_errors JSONB,        -- list of rule failures
  ocr_engine VARCHAR,             -- xml_bypass | paddleocr | llm_fallback
  processing_time_ms INT,
  created_at
)

-- Per-field confidence — HITL correction target
field_confidences (
  id UUID PK, result_id FK, tenant_id FK,
  field_name, raw_value, confidence FLOAT,
  is_corrected BOOL DEFAULT FALSE,
  corrected_value, corrected_by, corrected_at,
  exported BOOL DEFAULT FALSE     -- tracks feedback loop export status
)

-- Human review queue
hitl_queue (
  id UUID PK, document_id FK, tenant_id FK,
  reason VARCHAR,                 -- low_confidence | validation_error | manual_flag
  status VARCHAR,                 -- pending | in_review | resolved
  assigned_to, created_at, resolved_at, notes
)

-- Compliance trail — 10-year retention
audit_log (
  id UUID PK, tenant_id FK, document_id FK NULL,
  action VARCHAR,                 -- upload | processed | corrected | exported | key_rotated
  actor VARCHAR,                  -- system | admin_user_id | api_key_label
  details JSONB, created_at
)

-- Admin panel users
admin_users (
  id UUID PK, tenant_id FK NULL,  -- NULL = super_admin (cross-tenant)
  email UNIQUE, password_hash,
  role VARCHAR,                   -- super_admin | tenant_admin | reviewer
  is_active BOOL, created_at
)
```

**Indexes:** `(tenant_id, created_at)` on every tenant-scoped table. `(document_id)` on `extraction_results`, `field_confidences`, `hitl_queue`.

### MinIO bucket layout

```
e-invoice-raw/
  {tenant_id}/{document_id}/original.{pdf|xml|jpg|png}

e-invoice-processed/
  {tenant_id}/{document_id}/extracted.json
  {tenant_id}/{document_id}/thumbnail.jpg     # for HITL overlay UI

e-invoice-models/
  det/{version}/inference.pdmodel + .pdiparams
  rec/{version}/inference.pdmodel + .pdiparams
  table/{version}/inference.pdmodel + .pdiparams
  layout/{version}/inference.pdmodel + .pdiparams
  sr/{version}/espcn.onnx
```

### Redis key layout

```
rate:{tenant_id}:{window_minute}   → request count          TTL: 60s
task:{celery_task_id}              → status + result cache   TTL: 24h
```

---

## 5. Document Pre-processing Pipeline

Runs in `preprocessor.py` before any OCR model is invoked. All steps are CPU-bound and run in `ThreadPoolExecutor`.

| Step | Problem solved | Implementation |
|---|---|---|
| Orientation correction | 90°/180°/270° rotation | PaddleOCR cls model |
| Perspective correction | Photo taken at angle | 4-point contour → warpPerspective |
| Deskew | Small rotational drift | Hough line transform |
| Denoising | Scanner grain, thermal artifacts | `fastNlMeansDenoising` / bilateral filter |
| Contrast enhancement | Shadow, faded text | CLAHE |
| Binarization | Mixed background | Sauvola adaptive thresholding |
| Super-resolution | DPI < 150 (mobile photo, old fax) | ESPCN ONNX (lightweight CPU SR) |
| Border crop | Scanner edges | Contour detection → crop |

`PreprocessResult` records which steps fired — stored in `raw_fields` for debugging and as a retraining signal.

---

## 6. Self-Built PaddleOCR Engine

### Four model components

```
Invoice image
     ↓
Layout Analyzer (PP-StructureV2)
→ identifies: text_block, table, title, figure regions
     ↓
   Text regions              Table regions
     ↓                            ↓
Detection (DBNet+ResNet18)    SLANet
→ text bounding boxes         → cell coordinates + structure
     ↓                            ↓
Recognition (SVTRv2, VN charset)
→ text string + char-level confidence per crop
```

### Model targets

| Model | Base checkpoint | Fine-tune data | Metric target |
|---|---|---|---|
| Detection (DBNet) | `ch_PP-OCRv4_det` | Annotated VN invoice images | Hmean ≥ 0.92 |
| Recognition (SVTRv2) | `ch_PP-OCRv4_rec` | VN crops + synthetic | CER ≤ 0.02 |
| Table (SLANet) | `SLANet_ch` | Invoice table annotations | TEDS ≥ 0.90 |
| Layout | `PP-StructureV2` | Invoice layout annotations | mAP ≥ 0.85 |

### Async wrapper pattern

```python
class PaddleOCREngine:
    def __init__(self):
        # All models loaded once at worker startup
        self._det = self._rec = self._table = self._layout = None
        self._executor = ThreadPoolExecutor(max_workers=4)

    async def extract(self, image: np.ndarray) -> ExtractionResult:
        loop = asyncio.get_event_loop()
        layout = await loop.run_in_executor(self._executor, self._run_layout, image)

        # Concurrent detection+recognition across all regions
        text_results = await asyncio.gather(*[
            loop.run_in_executor(self._executor, self._run_det_rec, r)
            for r in layout.text_regions
        ])
        table_results = await asyncio.gather(*[
            loop.run_in_executor(self._executor, self._run_table, r)
            for r in layout.table_regions
        ])
        return self._merge(text_results, table_results)
```

`asyncio.gather` parallelises per-region work while keeping the event loop free.

### HITL → retraining feedback loop

```
Reviewer corrects field in admin panel
  → field_confidences.is_corrected = TRUE, corrected_value saved
  → Nightly Celery beat: feedback/export_corrections.py
      pulls WHERE is_corrected=TRUE AND exported=FALSE
      → writes PPOCRLabel annotation files
      → marks exported=TRUE
  → feedback/merge_dataset.py merges into data/annotated/ + re-splits
  → scripts/train_all.sh (manual or weekly schedule)
  → scripts/evaluate.py must pass CER/Hmean/TEDS thresholds
  → scripts/push_model.py → MinIO e-invoice-models/{model}/v{N+1}/
  → ocr-worker hot-reloads on next task batch
```

---

## 7. Full OCR Pipeline Flow

```
core-api  ──upload──►  validate mime/size
                        SHA-256 dedup check  ──duplicate?──► return existing result
                        store raw file → MinIO (aiobotocore async)
                        INSERT document (status='queued') → PostgreSQL (asyncpg)
                        dispatch Celery task → Redis
                        return { document_id, task_id }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ocr-worker  ◄──task──  asyncio.run(_pipeline(document_id))
                              │
                        detect_format()     ← async, reads file header from MinIO
                             / \
                          XML   PDF / Image
                           │         │
                    xml_parser()   preprocessor.run()     ← image correction
                    (conf=1.0)     pdf_to_images()        ← run_in_executor
                                   ocr_engine.extract()   ← run_in_executor + gather
                           │         │
                     fields +    fields + per-field confidence scores
                             \ /
                        validate(fields, tenant_rules)
                             │
                      avg_confidence ≥ tenant.threshold?
                            / \
                          YES   NO
                           │     │
                           │   llm_fallback()   ← httpx.AsyncClient → Claude Vision
                           │     │                (sends only low-confidence fields)
                           │   re-score
                           │     / \
                           │   YES   NO (still below threshold)
                           │    │      │
                           │    │   push hitl_queue, status='hitl'
                            \  /
                        store extraction_results + field_confidences
                        write audit_log
                        UPDATE document.status = 'done'
                        fire webhook (asyncio.create_task — non-blocking)
```

### Coroutine strategy

| Operation | Pattern |
|---|---|
| FastAPI endpoints | `async def` — native |
| PostgreSQL | SQLAlchemy 2.0 `async_session` + `asyncpg` driver |
| MinIO file I/O | `aiobotocore` async S3-compatible client |
| PaddleOCR (CPU-bound) | `await loop.run_in_executor(thread_pool, fn, args)` |
| PDF → image conversion | `run_in_executor` (pdfplumber is blocking) |
| Image preprocessing | `run_in_executor` (OpenCV is blocking) |
| Claude Vision API | `httpx.AsyncClient` with timeout + retry |
| Celery task entry | `asyncio.run(_pipeline(...))` — own event loop per task |
| Webhook delivery | `asyncio.create_task()` — fire-and-forget |

### Confidence scoring

```
confidence_score = (1/N) × Σ confidence_field_k

≥ tenant.confidence_threshold (default 0.95) → status = done
<  threshold, after LLM retry                → status = hitl
XML bypass path                              → all fields confidence = 1.0
```

### LLM fallback contract (minimises token cost)

```json
{
  "image_base64": "...",
  "low_confidence_fields": ["seller_tax_code", "total_amount"],
  "doc_type": "vat_invoice",
  "already_extracted": { "invoice_number": "...", "date": "..." }
}
```

---

## 8. API Contract

### Authentication

| Actor | Scheme | Detail |
|---|---|---|
| Tenant / caller | API Key | `Authorization: Bearer einv_{slug}_{32_random_chars}` — stored as SHA-256 hash |
| Admin user | JWT | 15-min access token + 7-day refresh, `role` claim embedded |

Rate limiting enforced in Redis per `tenant_id` per 60-second window.

### core-api endpoints (tenant-authenticated)

```
POST   /v1/documents/upload
       multipart/form-data { file, doc_type? }
       → 202 { document_id, task_id, status: "queued" }

GET    /v1/documents/{document_id}
       → 200 { document_id, status, doc_type, created_at, processed_at }

GET    /v1/documents/{document_id}/result
       → 200 { document_id, confidence_score, ocr_engine,
               validated_fields, validation_errors, field_confidences }
       → 404 if not yet done

GET    /v1/documents?status=&doc_type=&from=&to=&page=&limit=

POST   /v1/webhooks/test
```

### Webhook payload (outbound)

```json
{
  "event": "document.processed",
  "document_id": "uuid",
  "status": "done | hitl | error",
  "confidence_score": 0.97,
  "ocr_engine": "xml_bypass | paddleocr | llm_fallback",
  "result_url": "/v1/documents/{id}/result",
  "timestamp": "2026-05-24T10:00:00Z"
}
```

### admin-api endpoints (JWT-authenticated, role-gated)

```
# Auth
POST   /admin/auth/login                    → { access_token, refresh_token }
POST   /admin/auth/refresh                  → { access_token }

# HITL queue  [reviewer+]
GET    /admin/hitl?status=&tenant_id=&page=
GET    /admin/hitl/{document_id}            → image URL + all field_confidences
PATCH  /admin/hitl/{document_id}/fields     → { corrections: [{field_name, corrected_value}] }
POST   /admin/hitl/{document_id}/resolve

# Flow control  [tenant_admin+]
GET    /admin/queue/stats                   → { queued, processing, done_today, hitl_pending }
POST   /admin/queue/pause
POST   /admin/queue/resume
PATCH  /admin/tenants/{id}/config           → confidence_threshold, webhook_url, rules

# Tenant management  [super_admin]
GET    /admin/tenants
POST   /admin/tenants
PATCH  /admin/tenants/{id}
POST   /admin/tenants/{id}/api-keys
DELETE /admin/tenants/{id}/api-keys/{key_id}

# Users  [tenant_admin+]
GET    /admin/users
POST   /admin/users
PATCH  /admin/users/{id}
```

### Role hierarchy

```
super_admin   → everything, all tenants
tenant_admin  → own tenant: config, users, queue control
reviewer      → own tenant: HITL queue only
```

---

## 9. Error Handling

### Error response envelope

```json
{
  "error": {
    "code": "LOW_CONFIDENCE_ROUTED_TO_HITL",
    "message": "Document confidence 0.81 is below tenant threshold 0.95",
    "document_id": "uuid"
  }
}
```

### Error categories

| Category | Examples | Strategy |
|---|---|---|
| Input (4xx) | Wrong mime, file too large, duplicate SHA-256 | Reject immediately, no task dispatched |
| Pipeline failure | PaddleOCR crash, corrupt PDF | Celery retry ×3, backoff: 10s → 60s → 300s, then `status=error` |
| LLM timeout | Claude API slow/down | 2 retries (5s, 15s), then skip LLM → route to HITL directly |
| Storage unreachable | MinIO down | Reject upload with 503, no partial state written |
| DB down | PostgreSQL unreachable | FastAPI lifespan health check, 503 on startup |

**Invariant:** a document is never left permanently in `processing` status. All retry exhaustion paths write `status=error` + `audit_log` entry.

---

## 10. Observability

- **Structured logging:** `structlog` JSON lines — every line carries `tenant_id`, `document_id`, `task_id`, `ocr_engine`
- **Trace propagation:** `X-Request-ID` header flows from `core-api` → Celery task metadata → `admin-api`
- **Metrics:** `prometheus-fastapi-instrumentator` on each FastAPI service

### Key metrics

```
documents_processed_total          {tenant, engine, status}   counter
processing_duration_seconds        {engine}                   histogram
confidence_score_histogram         {engine}                   histogram
hitl_queue_depth                   {tenant}                   gauge
celery_queue_depth                                            gauge
llm_fallback_invocations_total     {result: success|hitl}     counter
api_requests_total                 {endpoint, status_code}    counter
```

---

## 11. Infrastructure & Deployment

### Docker Compose services

```yaml
services:
  nginx:          # reverse proxy: /v1/* → core-api:8000, /admin/* → admin-api:8001
  core-api:       # FastAPI, 2 uvicorn workers
  admin-api:      # FastAPI, 1 uvicorn worker
  ocr-worker:     # Celery — scale replicas independently
  celery-beat:    # scheduled jobs: feedback export, audit log archival, model reload
  postgres:       # postgres:16-alpine, pgdata volume, daily pg_dump → MinIO
  redis:          # redis:7-alpine, Celery broker + result backend + rate limiting
  minio:          # object storage: e-invoice-raw, e-invoice-processed, e-invoice-models
```

**Scale path A → B:** `docker compose up --scale ocr-worker=4` — no architecture change.

**Alembic** manages all migrations. First migration creates all tables + `(tenant_id, created_at)` indexes.

### Requirements split

```
requirements/base.txt        sqlalchemy[asyncio], asyncpg, pydantic, httpx, structlog,
                             aiobotocore, celery, redis, alembic, prometheus-fastapi-instrumentator
requirements/core-api.txt    fastapi, uvicorn, python-multipart, pyjwt, passlib
requirements/ocr-worker.txt  paddlepaddle, paddleocr, paddlex, opencv-python-headless,
                             scikit-image, imutils, deskew, pdfplumber, pdf2image, Pillow,
                             onnxruntime, numpy
requirements/admin-api.txt   fastapi, uvicorn, pyjwt, passlib
requirements/training.txt    paddlepaddle, paddleocr, paddle2onnx, text-renderer,
                             scikit-image, Pillow, numpy, opencv-python,
                             jupyter, matplotlib, seaborn, onnxruntime
```

---

## 12. Testing Strategy

| Layer | Tool | Scope |
|---|---|---|
| Unit | `pytest` + `pytest-asyncio` | XML parser, each validator rule, scorer, extractor |
| Integration | `testcontainers` (real PG + MinIO) | Full pipeline on fixture documents |
| API | `httpx.AsyncClient` + FastAPI `TestClient` | Every endpoint, auth enforcement, tenant isolation |
| Load | `locust` | Upload burst, queue depth under sustained load |

**Fixture documents:**
- Clean XML VAT invoice (XML bypass path)
- Good-quality PDF scan (PaddleOCR path, expect done)
- Low-quality scan (exercises LLM fallback + HITL routing)

---

## 13. v1 → v2+ Extension Path

The pipeline is document-type-agnostic. Adding BOL, Packing List, Freight Invoice, POD in v2 requires:

1. Add new `extractor.py` logic for the doc type
2. Add new validation rules in `validator.py`
3. Add new fields to `label_schema.json` in `training/annotation/`
4. No changes to `core-api`, data model, or infrastructure

---

## 14. Open Questions (resolve before implementation)

- [ ] GPU availability on VPS? Affects PaddleOCR inference speed and whether `paddlepaddle-gpu` is used
- [ ] Vietnamese character set completeness — confirm SVTRv2 covers all diacritics + invoice-specific symbols
- [ ] Claude Vision model selection — `claude-opus-4-7` for max accuracy vs `claude-haiku-4-5` for cost at fallback
- [ ] Alembic migration strategy for adding new doc types without downtime
- [ ] Backup schedule and MinIO retention policy for 10-year audit log compliance
