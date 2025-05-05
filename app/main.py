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

app = FastAPI(
    title="PDFConverterAI API",
    description="API for PDFConverterAI.com tools and RapidAPI distribution",
    version="1.0",
    docs_url="/docs",
    openapi_url="/openapi.json"
)

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
    return {"message": "PDFConverterAI API is running"}

@app.get("/health", summary="Health check")
@limiter.limit("5/minute")
async def health_check(request: Request):
    return {"status": "healthy"}

@app.get("/ping", summary="Ping endpoint")
@limiter.limit("5/minute")
async def ping(request: Request):
    return {"message": "pong"}