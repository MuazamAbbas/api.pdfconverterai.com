from fastapi import APIRouter, Depends
import logging
from app.core.security import verify_api_key
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

router = APIRouter(prefix="/categories", tags=["categories"])

@router.get("/test", summary="Test Categories endpoint")
async def test_categories():
    logger.debug("🧪 Testing Categories endpoint")
    return {"message": "Categories router is working"}

@router.get("/list", summary="List available tool categories")
async def list_categories(api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Listing tool categories")
    try:
        categories = [
            "ai_tools", "seo_tools", "web_tools", "downloaders", "unit_converters",
            "binary_tools", "calculators", "cyber_security", "miscellaneous",
            "pdf", "text", "image", "video"
        ]
        logger.debug("✅ Categories listed: %s", categories)
        return {"categories": categories}
    except Exception as e:
        logger.exception("💥 Error listing categories: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error listing categories: {str(e)}")