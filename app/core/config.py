from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    app_name: str = "PDFConverterAI"
    app_env: str = "production"
    log_level: str = "INFO"
    database_url: str
    allowed_origins: str = "https://pdfconverterai.com,https://api.pdfconverterai.com"
    model_path: str = "/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/models"
    # File/job metadata retention window (Handbook Part C.1: 30-60 min temp file
    # lifecycle). Drives the TTL indexes on files.expiresAt and jobs.expiresAt so
    # Mongo cleanup stays in sync with the filesystem worker's cleanup.
    file_retention_minutes: int = 60
    # ARQ job queue / cache backend (Handbook Part C.2, ADR-006). Local dev
    # points at a local Redis; production points at the VPS's local Redis
    # via .env (deployment-time concern, not hardcoded here).
    redis_url: str = "redis://localhost:6379"
    # Upload validation (Handbook Part C.10: MIME+extension+size+magic-bytes+
    # sanitized filenames are all required layers). Enforced in
    # `app/services/files/service.py::save_uploaded_file` before a file is
    # fully buffered into memory.
    max_upload_size_mb: int = 25

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()