import logging

from app.core.logging import setup_logging

# Must run before any router/service import below - those modules call
# `logging.getLogger(__name__)` at import time, and whichever handler setup
# runs first used to silently win over the rest (see app/core/logging.py).
setup_logging()

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402
from arq.connections import RedisSettings, create_pool  # noqa: E402
from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402
from transformers import pipeline  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import ensure_indexes  # noqa: E402
from app.core.rate_limiter import limiter  # noqa: E402
from app.core.security import verify_api_key  # noqa: E402
from app.core.storage import cleanup_expired_files  # noqa: E402
from app.routers import (  # noqa: E402
    admin,
    ai_tools,
    auth,
    binary_tools,
    calculators,
    categories,
    cyber_security,
    downloaders,
    files,
    image,
    jobs,
    miscellaneous,
    pdf,
    seo_tools,
    text,
    tools,
    unit_converters,
    video,
    web_tools,
)
from app.shared.responses import redact_validation_errors  # noqa: E402

logger = logging.getLogger(__name__)

app = FastAPI(
    title="PDFConverterAI API",
    description="API for PDFConverterAI.com tools and RapidAPI distribution",
    version="1.0",
    docs_url="/docs",
    openapi_url="/openapi.json"
)


def _load_pipeline(state_attr: str, task: str, model: str, device: str = "cpu"):
    """Load and verify one Hugging Face pipeline for `app.state` (Handbook
    Part D.1 - avoid copy-pasted load/verify blocks per model). Called once
    per model from `startup_event()` below, not at module scope, so it runs
    inside FastAPI's own startup hook rather than before `app` is fully
    configured.
    """
    logger.debug("Loading %s: %s", state_attr, model)
    try:
        pl = pipeline(task, model=model, device=device)
    except Exception as e:
        logger.error("❌ Failed to load %s: %s", state_attr, str(e))
        raise
    if not hasattr(pl, "model"):
        logger.error("❌ %s is invalid or not initialized", state_attr)
        raise ValueError(f"Invalid {state_attr}")
    logger.debug("✅ %s loaded and verified", state_attr)
    return pl


# Add CORS middleware for RapidAPI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiter (Limiter instance itself now lives in app/core/rate_limiter.py
# so app/routers/auth.py can import and reuse the same instance for
# POST /auth/login's brute-force mitigation).
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Dynamic rate-limiting based on key type
async def get_rate_limit(key_info: dict = Depends(verify_api_key)):
    key_data = key_info["key_data"]
    if key_data["type"] == "internal":
        return limiter.limit("100/minute")
    else:
        return limiter.limit(f"{key_data['rate_limit_per_day']}/day")

# Apply dependencies to protected routes
protected_dependency = [Depends(verify_api_key), Depends(get_rate_limit)]

# Include routers with /v1 prefix and tags
#
# auth is deliberately NOT given `protected_dependency` (verify_api_key):
# it is a brand-new, separate human-admin login surface (JWT-via-httpOnly-
# cookie), not the existing service-to-service x-api-key mechanism - see
# app/core/security.py's verify_api_key (untouched) vs
# app/core/admin_auth.py's require_admin (new). POST /auth/login is the
# entry point human operators use *before* they have any credential, so it
# can never itself require one; POST /auth/logout is intentionally
# unauthenticated too (always succeeds so a client can clear stale/expired
# cookie state either way).
app.include_router(auth.router, prefix="/v1", tags=["Auth"])
# `admin` (ADR-019, Homepage Sections CMS) - two APIRouter instances on
# purpose (see app/routers/admin.py's module docstring for the full
# reasoning): `admin.public_router` is the single unauthenticated
# GET /v1/admin/homepage-sections route (no x-api-key, no admin session -
# spec requirement, the public homepage reads this with no credential).
# `admin.router` is every other route (list-all/create/update/reorder/
# delete) and gets `protected_dependency` here exactly like every other
# tool router below - NOT the bare-router exemption `auth.router` gets,
# since `admin` has no login chicken-and-egg problem to justify skipping
# the router-level x-api-key check. Each of those routes also carries its
# own `Depends(require_admin)`, so writes end up with both layers
# (ADR-008 Secure by Default) rather than relying on either alone.
app.include_router(admin.public_router, prefix="/v1", tags=["Admin"])
app.include_router(admin.router, prefix="/v1", tags=["Admin"], dependencies=protected_dependency)
app.include_router(ai_tools.router, prefix="/v1", tags=["AI Tools"], dependencies=protected_dependency)
app.include_router(seo_tools.router, prefix="/v1", tags=["SEO Tools"], dependencies=protected_dependency)
app.include_router(web_tools.router, prefix="/v1", tags=["Web Tools"], dependencies=protected_dependency)
app.include_router(downloaders.router, prefix="/v1", tags=["Downloaders"], dependencies=protected_dependency)
app.include_router(unit_converters.router, prefix="/v1", tags=["Unit Converters"], dependencies=protected_dependency)
app.include_router(binary_tools.router, prefix="/v1", tags=["Binary Tools"], dependencies=protected_dependency)
app.include_router(calculators.router, prefix="/v1", tags=["Calculators"], dependencies=protected_dependency)
app.include_router(cyber_security.router, prefix="/v1", tags=["Cyber Security"], dependencies=protected_dependency)
app.include_router(miscellaneous.router, prefix="/v1", tags=["Miscellaneous"], dependencies=protected_dependency)
app.include_router(pdf.router, prefix="/v1", tags=["PDF Tools"], dependencies=protected_dependency)
app.include_router(text.router, prefix="/v1", tags=["Text Tools"], dependencies=protected_dependency)
app.include_router(image.router, prefix="/v1", tags=["Image Tools"], dependencies=protected_dependency)
app.include_router(video.router, prefix="/v1", tags=["Video Tools"], dependencies=protected_dependency)
app.include_router(categories.router, prefix="/v1", tags=["Categories"], dependencies=protected_dependency)
app.include_router(tools.router, prefix="/v1", tags=["Tools"], dependencies=protected_dependency)
app.include_router(files.router, prefix="/v1", tags=["Files"], dependencies=protected_dependency)
app.include_router(jobs.router, prefix="/v1", tags=["Jobs"], dependencies=protected_dependency)

# Standard response envelope (Handbook Part C.5, ADR-004): every error
# response - old routes raising a plain-string HTTPException or new ones
# raising `app.shared.responses.api_error(...)` - comes back as
# {success: false, message, error: {code}}. Never leaks a stack trace
# (Part C.10).
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "success" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "message": str(detail), "error": {"code": "HTTP_ERROR"}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # A structurally missing/malformed required field (e.g. no `X-API-Key`
    # header at all) hits this handler instead of StarletteHTTPException -
    # without it, callers got FastAPI's stock {"detail": [...]} shape here
    # while an *invalid* API key value correctly got the standard envelope.
    #
    # `exc.errors()` includes pydantic's raw submitted `input` value for every
    # failing field by default - redacted here before logging (see
    # app/shared/responses.py::redact_validation_errors docstring for why:
    # an over-length/malformed POST /v1/auth/login body would otherwise
    # write the plaintext attempted password straight into error.log).
    # Applied globally, not just for auth, since any endpoint's body could
    # contain an equally sensitive field going forward.
    logger.warning(
        "Request validation failed on %s: %s",
        request.url.path,
        redact_validation_errors(exc.errors()),
    )
    return JSONResponse(
        status_code=422,
        content={"success": False, "message": "Invalid request", "error": {"code": "VALIDATION_ERROR"}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s: %s", request.url.path, str(exc))
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": "Internal server error", "error": {"code": "INTERNAL_ERROR"}},
    )

@app.get("/", summary="Root endpoint")
async def read_root():
    logger.debug("📡 Accessing root endpoint")
    return {"message": "PDFConverterAI API is running"}

@app.get("/health", summary="Health check")
@limiter.limit("5/minute")
async def health_check(request: Request):
    logger.debug("🩺 Accessing health check endpoint")
    return {"status": "healthy"}

@app.get("/ping", summary="Ping endpoint")
@limiter.limit("5/minute")
async def ping(request: Request):
    logger.debug("🏓 Accessing ping endpoint")
    return {"message": "pong"}

scheduler = AsyncIOScheduler()


@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Starting PDFConverterAI API")
    await ensure_indexes()

    # Preload essential Hugging Face models (moved here from module scope so
    # loading happens inside the startup hook, after `app` is fully
    # configured, instead of before - ADR-015 Open Item 2).
    #
    # `paraphrase_pipeline`/`summarize_pipeline` no longer load here: now
    # that `POST /text/paraphrase`, `POST /text/summarize`, and
    # `POST /web_tools/summarize` are Tier 2 jobs (Handbook Part I.2) run
    # from the ARQ worker process instead of this FastAPI request process,
    # those two t5-small pipelines are loaded once per worker process in
    # `app/worker.py`'s `on_startup()` instead, via `ctx["paraphrase_pipeline"]`/
    # `ctx["summarize_pipeline"]`.
    logger.info("🚀 Starting to preload essential Hugging Face models")
    app.state.financial_sentiment_pipeline = _load_pipeline(
        "financial_sentiment_pipeline",
        "sentiment-analysis",
        "distilbert-base-uncased-finetuned-sst-2-english",
    )
    logger.info("✅ Essential models preloaded successfully")

    # ARQ job queue (Handbook Part C.2, ADR-006): one shared connection pool
    # for the process, used by pdf/convert|to_word|summarize to enqueue jobs
    # for app/worker.py to pick up.
    app.state.arq_redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    logger.debug("✅ ARQ redis pool connected")

    # Files/jobs retention sweep (Handbook Part C.1): the TTL index on
    # files.expiresAt only expires the Mongo record, not the bytes on disk -
    # this periodically deletes both, extending the pre-existing
    # APScheduler cleanup pattern instead of introducing a new one.
    scheduler.add_job(cleanup_expired_files, "interval", minutes=5, id="cleanup_expired_files")
    scheduler.start()

    logger.debug("✅ Startup event completed")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🛑 Shutting down PDFConverterAI API")
    try:
        if hasattr(app.state, "financial_sentiment_pipeline"):
            logger.debug("🧹 Cleaning up financial_sentiment_pipeline")
            del app.state.financial_sentiment_pipeline
        if hasattr(app.state, "arq_redis"):
            logger.debug("🧹 Closing ARQ redis pool")
            await app.state.arq_redis.close()
        scheduler.shutdown(wait=False)
        logger.debug("✅ Shutdown event completed")
    except Exception as e:
        logger.error("❌ Error during shutdown: %s", str(e))