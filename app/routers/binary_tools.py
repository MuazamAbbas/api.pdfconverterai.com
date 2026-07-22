from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
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

router = APIRouter(prefix="/binary_tools", tags=["binary_tools"])

class TextToBinaryRequest(BaseModel):
    text: str

@router.get("/test", summary="Test Binary Tools endpoint")
async def test_binary_tools():
    logger.debug("🧪 Testing Binary Tools endpoint")
    return {"message": "Binary Tools router is working"}

@router.post("/text_to_binary", summary="Convert text to binary")
async def text_to_binary(request: TextToBinaryRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting text to binary: %s", request.text)
    if not request.text:
        logger.error("❌ Text is required")
        raise HTTPException(status_code=400, detail="Text is required")
    try:
        binary = ' '.join(format(ord(char), '08b') for char in request.text)
        logger.debug("✅ Text converted to binary: %s", binary)
        return {"text": request.text, "binary": binary}
    except Exception as e:
        logger.exception("💥 Error converting text to binary: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting text to binary: {str(e)}")