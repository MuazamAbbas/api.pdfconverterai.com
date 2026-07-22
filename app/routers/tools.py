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

router = APIRouter(prefix="/tools", tags=["tools"])

@router.get("/test", summary="Test Tools endpoint")
async def test_tools():
    logger.debug("🧪 Testing Tools endpoint")
    return {"message": "Tools router is working"}

@router.get("/list", summary="List available tools")
async def list_tools(api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Listing available tools")
    try:
        tools = {
            "pdf": ["upload", "convert"],
            "ai_tools": ["summarize"],
            "seo_tools": ["keyword_density"],
            "web_tools": ["url_encode"],
            "unit_converters": ["length"],
            "binary_tools": ["text_to_binary"],
            "calculators": ["calculate"],
            "cyber_security": ["password_generator"],
            "miscellaneous": ["timestamp"],
            "text": ["to_uppercase"]
        }
        logger.debug("✅ Tools listed: %s", tools)
        return {"tools": tools}
    except Exception as e:
        logger.exception("💥 Error listing tools: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error listing tools: {str(e)}")