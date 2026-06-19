# e-Invoice OCR

Production-grade Vietnamese invoice OCR system. Extracts structured charge line-items from scanned PDFs, images, and government e-invoice XML files. Self-hosted, multi-tenant, accuracy-first.

---

## Tech Stack

| Layer | Technology |
|---|---|
| **API framework** | [FastAPI](https://fastapi.tiangolo.com/) + Uvicorn (async) |
| **Task queue** | [Celery](https://docs.celeryq.dev/) + Redis broker |
| **OCR engine** | [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) — PP-OCRv4 rec (Vietnamese) + PPStructure SLANet table |
| **LLM fallback** | [Claude Sonnet](https://www.anthropic.com/) via Anthropic SDK (vision) |
| **Database** | PostgreSQL 16 + SQLAlchemy 2 async (asyncpg) |
| **Migrations** | Alembic |
| **Object storage** | MinIO (S3-compatible) — raw files, model registry, training crops |
| **Cache / broker** | Redis 7 |
| **Validation** | Pydantic v2 |
| **Auth** | PyJWT (HS256) + passlib/bcrypt |
| **Image processing** | OpenCV, NumPy, pdf2image, Pillow |
| **XML parsing** | defusedxml (DoS-safe) |
| **Logging** | structlog (structured JSON) |
| **Metrics** | Prometheus + prometheus-fastapi-instrumentator |
| **Experiment tracking** | MLflow |
| **Containerisation** | Docker + Docker Compose v2 |
| **Reverse proxy** | Nginx |
| **Language** | Python 3.11+ |

---

## Architecture

```
                         ┌──────────────┐
  Tenant client ────────▶│   core-api   │ FastAPI · document upload, status, results
                         └──────┬───────┘
                                │ Celery task
                                ▼
                         ┌──────────────┐
                         │  ocr-worker  │ Celery · three-path pipeline
                         │              │
                         │  1. XML      │ defusedxml parse → skip OCR entirely
                         │  2. PaddleOCR│ PPStructure table + PP-OCRv4 rec (vi)
                         │  3. LLM      │ Claude Sonnet vision fallback
                         └──────┬───────┘
                                │
                    ┌───────────┴────────────┐
                    ▼                        ▼
             ┌─────────────┐        ┌──────────────┐
             │  PostgreSQL │        │    MinIO     │
             │  (results,  │        │ (raw files,  │
             │   HITL,     │        │  models,     │
             │   training) │        │  training)   │
             └─────────────┘        └──────────────┘
                    ▲
                    │ review / correct
             ┌──────────────┐
             │  admin-api   │ FastAPI · HITL queue, tenant mgmt, model registry
             └──────────────┘
                    │ nightly
                    ▼
             ┌──────────────┐
             │   Training   │ prepare_dataset → finetune PP-OCRv4 → push_model
             └──────────────┘
```

### OCR routing

| Document type | Path | Trigger |
|---|---|---|
| Government e-invoice XML | XML bypass | `source_format = xml` |
| PDF / image (confidence ≥ threshold) | PaddleOCR | default |
| PDF / image (confidence < threshold) | LLM fallback (Claude) | auto |

Confidence threshold is per-tenant (default 0.95).

---

## Services

| Service | Port | Description |
|---|---|---|
| `core-api` | 8000 | Tenant-facing REST API — upload, status, result |
| `admin-api` | 8001 | Admin REST API — HITL, tenants, schemas, models |
| `ocr-worker` | — | Celery worker — OCR pipeline |
| `postgres` | 5432 | Primary database |
| `redis` | 6379 | Celery broker + result backend |
| `minio` | 9000 / 9001 | Object storage + console |
| `nginx` | 80 / 443 | Reverse proxy (production) |

---

## Quick Start

### 1. Prerequisites

- Docker + Docker Compose v2
- 4 GB RAM minimum (8 GB recommended for LLM fallback)

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum set SECRET_KEY and CLAUDE_API_KEY
openssl rand -hex 32   # paste as SECRET_KEY
```

### 3. Start all services

```bash
docker compose up -d
```

### 4. Run database migrations

```bash
docker compose exec admin-api alembic upgrade head
```

### 5. Create the first super-admin

```bash
docker compose exec admin-api python -m app.management create-superuser \
  --email admin@yourcompany.com \
  --password <strong-password>
```

### 6. Verify

```bash
curl http://localhost:8000/health/ready   # core-api
curl http://localhost:8001/health/ready   # admin-api
```

---

## Environment Variables

Copy `.env.example` to `.env` and set the required values:

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✓ | PostgreSQL async URL (`postgresql+asyncpg://...`) |
| `REDIS_URL` | ✓ | Redis URL |
| `MINIO_ENDPOINT` | ✓ | MinIO host:port |
| `MINIO_ACCESS_KEY` | ✓ | MinIO access key |
| `MINIO_SECRET_KEY` | ✓ | MinIO secret key |
| `SECRET_KEY` | ✓ | JWT signing secret (generate with `openssl rand -hex 32`) |
| `CLAUDE_API_KEY` | ✓ | Anthropic API key (LLM fallback) |
| `CLAUDE_MODEL` | | Default: `claude-sonnet-4-6` |
| `OCR_DEFAULT_CONFIDENCE_THRESHOLD` | | Default: `0.95` |
| `ENVIRONMENT` | | `development` (shows `/docs`) or `production` |

---

## API Overview

### core-api — tenant-facing

```
POST   /v1/documents                 Upload document (PDF, image, XML)
GET    /v1/documents/{id}/status     Poll processing status
GET    /v1/documents/{id}/result     Retrieve extraction result
```

Authentication: `Authorization: Bearer <api_key>` (API key issued by admin-api)

### admin-api — admin UI

```
# Auth
POST   /admin/auth/login             Email + password → JWT tokens
POST   /admin/auth/refresh           Rotate refresh token
GET    /admin/auth/me                Current user info

# Tenants
GET    /admin/tenants                List all tenants (super_admin)
POST   /admin/tenants                Create tenant
PATCH  /admin/tenants/{id}           Update confidence threshold / webhook URL
POST   /admin/tenants/{id}/api-keys  Issue API key (raw key returned once)

# HITL review
GET    /admin/review-queue           List pending reviews (filterable by status)
GET    /admin/review-queue/{id}      Detail with full extraction + field confidences
PATCH  /admin/review-queue/{id}/assign        Assign reviewer
PATCH  /admin/review-queue/{id}/corrections   Apply field + line-item corrections
PATCH  /admin/review-queue/{id}/resolve       Mark resolved

# Direct field correction
PATCH  /admin/field-corrections/{fc_id}  Correct a single FieldConfidence record

# Schema management
GET    /admin/schemas                List schema versions
POST   /admin/schemas                Create new version (snapshot of label_schema.json)
POST   /admin/schemas/{id}/activate  Activate version (hot-reloads column mapper)
GET    /admin/column-aliases         Unmatched column headers from production
PATCH  /admin/column-aliases/{id}    Approve / reject alias proposal

# Model registry
GET    /admin/models                 List model versions (filter by type)
PATCH  /admin/models/{id}/activate   Promote model version (det/rec/table/layout)
```

Swagger UI available at `http://localhost:8001/docs` when `ENVIRONMENT=development`.

---

## HITL → Finetuning Loop

When reviewers correct OCR errors in the admin UI, corrections accumulate in `field_confidences` and `invoice_line_items` (with `is_corrected=True`). A nightly Celery beat task exports uncorrected records to MinIO.

To retrain the Vietnamese recognition model on accumulated corrections:

```bash
# 1. Prepare image crops + split files from DB corrections
python training/scripts/prepare_dataset.py \
  --output-dir training/data/splits \
  --max-samples 20000

# 2. Generate finetuning config (PP-OCRv4 Vietnamese rec)
python training/scripts/generate_finetune_config.py \
  --data-dir training/data/splits \
  --pretrained training/pretrain_models/vi_PP-OCRv4_rec_train/best_accuracy \
  --output configs/rec_vi_finetune.yml

# 3. Download pretrained checkpoint (first time only)
wget https://paddleocr.bj.bcebos.com/PP-OCRv4/vietnamese/vi_PP-OCRv4_rec_train.tar
tar -xf vi_PP-OCRv4_rec_train.tar -C training/pretrain_models/

# 4. Train
python /path/to/PaddleOCR/tools/train.py -c configs/rec_vi_finetune.yml

# 5. Export to inference format
python /path/to/PaddleOCR/tools/export_model.py \
  -c configs/rec_vi_finetune.yml \
  -o Global.pretrained_model=training/output/rec_vi_finetune/best_accuracy \
     Global.save_inference_dir=training/output/rec_vi_finetune/inference

# 6. Evaluate
python training/scripts/evaluate_model.py \
  --model-dir training/output/rec_vi_finetune/inference \
  --test-file  training/data/splits/test.txt

# 7. Push to model registry and promote
python training/scripts/push_model.py \
  --model-type rec \
  --model-dir  training/output/rec_vi_finetune/inference \
  --version    v1.1.0 --promote
```

Minimum recommended dataset: **500 corrected crops** for meaningful improvement, **2 000+** for reliable gains.

---

## Database Migrations

Migrations live in `migrations/versions/`. Run with:

```bash
# Apply all pending migrations
alembic upgrade head

# Roll back one step
alembic downgrade -1

# Generate a new migration from model changes
alembic revision --autogenerate -m "describe the change"
```

---

## Project Structure

```
e-Invoice/
├── common/einv_common/        Shared library (models, schemas, config, storage)
│   ├── models/                SQLAlchemy ORM models
│   └── schemas/               Pydantic v2 request/response schemas
├── services/
│   ├── core-api/              Tenant-facing FastAPI service
│   ├── admin-api/             Admin FastAPI service (HITL, JWT, tenants)
│   └── ocr-worker/            Celery worker + OCR pipeline
│       └── pipeline/          xml_parser, preprocessor, detector, extractor,
│                              ocr_engine, orchestrator, llm_fallback, …
├── training/
│   └── scripts/               prepare_dataset, generate_finetune_config,
│                              evaluate_model, push_model, promote_schema
├── migrations/versions/       Alembic migration files
├── configs/                   Generated PaddleOCR YAML configs (git-ignored output)
├── requirements/              Per-service requirements files
│   ├── base.txt               Shared deps (SQLAlchemy, Pydantic, Celery, …)
│   ├── core-api.txt
│   ├── admin-api.txt
│   ├── ocr-worker.txt
│   └── training.txt
├── docker-compose.yml
├── .env.example
└── alembic.ini
```

---

## Role Model

| Role | Access |
|---|---|
| `super_admin` | All tenants, all operations, model promotion |
| `tenant_admin` | Own tenant — update settings, issue API keys, review HITL |
| `reviewer` | Own tenant — HITL review and corrections only |

---

## Security Notes

- Webhook URLs are SSRF-validated: RFC-1918, loopback, and link-local ranges are blocked.
- API keys are stored as SHA-256 hashes only; the raw key is returned once at creation.
- XML documents are parsed with `defusedxml` to prevent billion-laughs DoS.
- JWT access tokens expire in 15 minutes; refresh tokens in 7 days (stateless).
- Admin API `/docs` is disabled in `ENVIRONMENT=production`.
