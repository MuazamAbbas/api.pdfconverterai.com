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
    # Shared error.log destination, consumed by `app.core.logging.setup_logging()`
    # (called once from `app/main.py`). Named `downloaders_log_path` from its
    # original introduction alongside the downloaders module (Handbook Part
    # C.3, ADR-015); kept as-is rather than renamed to avoid churning that
    # ADR and SPRINT_STATUS.md's existing references to this field.
    downloaders_log_path: str = (
        "/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"
    )
    # Node.js binary used by yt_dlp's `js_runtimes` option (via the
    # `yt-dlp-ejs` package) to solve YouTube's signature/n-parameter
    # challenges for the `web` player client - required, alongside the
    # `bgutil-ytdlp-pot-provider` PO Token server (`bgutil_pot_provider_url`
    # below), to get past YouTube's "Sign in to confirm you're not a bot"
    # IP-reputation check on this VPS (SPRINT_STATUS.md, 2026-07-30 finding).
    # A dedicated tarball install under /opt, not the system Node.js (Ubuntu
    # 24.04's apt package is 18.x; yt-dlp-ejs requires 22+).
    youtube_js_runtime_node_path: str = "/opt/nodejs/bin/node"
    # HTTP base URL of the bgutil-ytdlp-pot-provider companion service
    # (systemd unit `bgutil-pot.service`, mirroring `arq-worker.service`'s
    # shape) that generates PO Tokens for yt_dlp. Only needs setting here if
    # it's not on the default `127.0.0.1:4416` the plugin auto-detects -
    # kept as an explicit setting anyway (not relied on implicitly) so a
    # future non-default port/host doesn't silently fall back to no PO Token
    # provider at all.
    bgutil_pot_provider_url: str = "http://127.0.0.1:4416"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()