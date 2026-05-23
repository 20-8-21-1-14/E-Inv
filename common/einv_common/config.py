from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str
    database_pool_min: int = 5
    database_pool_max: int = 20

    # Redis / Celery
    redis_url: str
    celery_broker_url: str
    celery_result_backend: str

    # MinIO
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket_raw: str = "e-invoice-raw"
    minio_bucket_processed: str = "e-invoice-processed"
    minio_bucket_models: str = "e-invoice-models"
    minio_bucket_training: str = "e-invoice-training"
    minio_use_ssl: bool = False

    # Auth
    secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7

    # Claude
    claude_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    claude_timeout_seconds: int = 30
    claude_max_retries: int = 2

    # OCR
    ocr_thread_pool_size: int = 4
    ocr_default_confidence_threshold: float = 0.95
    ocr_model_dir: str = "/app/model_registry"

    # App
    environment: str = "development"
    log_level: str = "INFO"
    max_upload_size_mb: int = 50
    allowed_mime_types: str = (
        "application/pdf,text/xml,application/xml,image/jpeg,image/png,image/tiff"
    )

    @field_validator("allowed_mime_types", mode="before")
    @classmethod
    def parse_mime_types(cls, v: str) -> str:
        return v

    @property
    def allowed_mime_types_list(self) -> list[str]:
        return [m.strip() for m in self.allowed_mime_types.split(",")]

    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024


settings = Settings()
