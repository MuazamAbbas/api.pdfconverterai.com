import logging
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import verify_api_key
from app.shared.responses import api_error

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
        raise api_error(500, "Failed to generate password", "PASSWORD_GENERATION_FAILED")