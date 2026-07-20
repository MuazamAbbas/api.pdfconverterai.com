from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from app.core.security import verify_api_key
from app.routers import (
    ai_tools, seo_tools, web_tools, downloaders, unit_converters,
    binary_tools, calculators, cyber_security, miscellaneous,
    pdf, text, image, video, categories, tools, debug
)
import logging
from transformers import pipeline

# Logging setup
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="PDFConverterAI API",
    description="API for PDFConverterAI.com tools and RapidAPI distribution",
    version="1.0",
    docs_url="/docs",
    openapi_url="/openapi.json"
)

# Preload essential Hugging Face models
logger.info("🚀 Starting to preload essential Hugging Face models")
try:
    logger.debug("Loading paraphrase model: t5-small")
    app.state.paraphrase_pipeline = pipeline("text2text-generation", model="t5-small", device="cpu")
    logger.debug("✅ Paraphrase model loaded successfully")
except Exception as e:
    logger.error("❌ Failed to load paraphrase_pipeline: %s", str(e))
    raise

try:
    logger.debug("Loading summarization model: t5-small")
    app.state.summarize_pipeline = pipeline("summarization", model="t5-small", device="cpu")
    logger.debug("✅ Summarization model loaded successfully")
except Exception as e:
    logger.error("❌ Failed to load summarize_pipeline: %s", str(e))
    raise
logger.info("✅ Essential models preloaded successfully")

# Verify pipeline state
try:
    logger.debug("Verifying paraphrase_pipeline state")
    if not hasattr(app.state.paraphrase_pipeline, "model"):
        logger.error("❌ paraphrase_pipeline is invalid or not initialized")
        raise ValueError("Invalid paraphrase_pipeline")
    logger.debug("✅ paraphrase_pipeline state verified")
except Exception as e:
    logger.error("❌ paraphrase_pipeline verification failed: %s", str(e))
    raise

try:
    logger.debug("Verifying summarize_pipeline state")
    if not hasattr(app.state.summarize_pipeline, "model"):
        logger.error("❌ summarize_pipeline is invalid or not initialized")
        raise ValueError("Invalid summarize_pipeline")
    logger.debug("✅ summarize_pipeline state verified")
except Exception as e:
    logger.error("❌ summarize_pipeline verification failed: %s", str(e))
    raise

# Add CORS middleware for RapidAPI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiter
limiter = Limiter(key_func=get_remote_address)
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
app.include_router(debug.router, prefix="/v1", tags=["Debug"], dependencies=protected_dependency)

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

@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Starting PDFConverterAI API")
    logger.debug("✅ Startup event completed")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🛑 Shutting down PDFConverterAI API")
    try:
        if hasattr(app.state, "summarize_pipeline"):
            logger.debug("🧹 Cleaning up summarize_pipeline")
            del app.state.summarize_pipeline
        if hasattr(app.state, "paraphrase_pipeline"):
            logger.debug("🧹 Cleaning up paraphrase_pipeline")
            del app.state.paraphrase_pipeline
        logger.debug("✅ Shutdown event completed")
    except Exception as e:
        logger.error("❌ Error during shutdown: %s", str(e))