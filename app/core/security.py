import os
import logging
from fastapi import HTTPException, Header, Request
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime

from app.core.config import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

async def verify_api_key(x_api_key: str = Header(...), request: Request = None):
    logger.debug("🧪 Verifying API key")
    try:
        client = AsyncIOMotorClient(settings.database_url)
        db = client["pdfconverterai"]
        key_data = await db.api_keys.find_one({"key": x_api_key, "status": "active"})
        
        if not key_data:
            logger.warning("❌ No active key found for: %s", x_api_key)
            raise HTTPException(status_code=403, detail="Invalid API Key")
        
        # Check rate limit
        if key_data["usage_count"] >= key_data["rate_limit_per_day"]:
            logger.warning("🚫 Rate limit exceeded for key: %s", x_api_key)
            raise HTTPException(status_code=429, detail="Daily API rate limit exceeded")
        
        # Extract category from request path
        category = None
        if request:
            try:
                path = request.url.path
                if path.startswith("/v1/"):
                    parts = path.split("/")
                    if len(parts) > 2:
                        category = parts[2]  # e.g., "seo_tools" from "/v1/seo_tools/test"
            except Exception as e:
                logger.error("❌ Failed to extract category: %s", str(e))
                # Fallback: allow request to proceed without category check
                category = None
        
        # Check category access
        key_categories = key_data.get("categories", [])
        if category and "all" not in key_categories and category not in key_categories:
            logger.error("❌ API key not authorized for category: %s", category)
            raise HTTPException(status_code=403, detail=f"API key not authorized for category: {category}")
        
        # Update usage
        key_data["_id"] = str(key_data["_id"])
        await db.api_keys.update_one(
            {"key": x_api_key},
            {"$inc": {"usage_count": 1}, "$set": {"last_used": datetime.utcnow()}}
        )
        logger.debug("✅ Valid key found: id=%s", key_data.get("_id"))
        client.close()
        return {"key_data": key_data}
    except HTTPException:
        logger.debug("🔍 HTTPException raised, passing through")
        raise
    except Exception as e:
        logger.exception("💥 Error verifying API key %s: %s", x_api_key, str(e))
        raise HTTPException(status_code=500, detail="Internal server error")