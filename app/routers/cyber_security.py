from fastapi import APIRouter, HTTPException, Depends
import secrets
import string
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

router = APIRouter(prefix="/cyber_security", tags=["cyber_security"])

@router.get("/test", summary="Test Cyber Security endpoint")
async def test_cyber_security():
    logger.debug("🧪 Testing Cyber Security endpoint")
    return {"message": "Cyber Security router is working"}

@router.get("/password_generator", summary="Generate a random password")
async def password_generator(length: int = 12, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Generating password with length: %d", length)
    if length < 8 or length > 100:
        logger.error("❌ Invalid length: %d", length)
        raise HTTPException(status_code=400, detail="Length must be between 8 and 100")
    try:
        characters = string.ascii_letters + string.digits + string.punctuation
        password = ''.join(secrets.choice(characters) for _ in range(length))
        logger.debug("✅ Password generated")
        return {"password": password}
    except Exception as e:
        logger.exception("💥 Error generating password: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error generating password: {str(e)}")