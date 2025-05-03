from fastapi import APIRouter, Depends
import logging
from app.core.security import verify_api_key

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])

@router.get("/test", summary="Test Debug endpoint")
async def test_debug():
    logger.debug("🧪 Testing Debug endpoint")
    return {"message": "Debug router is working"}

@router.get("/auth", summary="Test API key authentication")
async def debug_auth(api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Testing API key authentication")
    return {"message": "✅ API Key is valid", "key_data": api_key["key_data"]}