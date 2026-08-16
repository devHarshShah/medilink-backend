from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_name: str = "MediLink Backend"
    app_version: str = "1.0.0"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://medilink:medilink@localhost:5432/medilink"

    # Auth
    secret_key: str = "change-me-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7  # frontend stores the cookie for 7 days

    # CORS. The frontend runs with withCredentials:false, so credentials are not allowed.
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # Redis (SSE pub/sub)
    redis_url: str = "redis://localhost:6379/0"

    # MinIO / S3
    s3_endpoint_url: str = "http://localhost:9000"
    s3_public_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "medilink"
    s3_secret_key: str = "medilink123"
    s3_bucket: str = "medilink-documents"
    s3_region: str = "us-east-1"

    # AI triage
    anthropic_api_key: str | None = None
    triage_model: str = "claude-sonnet-5"
    triage_timeout_seconds: float = 12.0

    # Uploads
    max_upload_bytes: int = 10 * 1024 * 1024
    allowed_upload_types: list[str] = ["image/jpeg", "image/png", "application/pdf"]

    # Queue engine
    queue_travel_speed_kmh: float = 25.0
    queue_safety_buffer_minutes: int = 10
    queue_rescore_interval_seconds: int = 30
    queue_reassign_hysteresis_minutes: float = 8.0
    queue_default_consultation_minutes: int = 12


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
