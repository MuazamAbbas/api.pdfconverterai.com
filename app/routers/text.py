from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
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

router = APIRouter(prefix="/text", tags=["text"])

class TextRequest(BaseModel):
    text: str

@router.get("/test", summary="Test Text Tools endpoint")
async def test_text():
    logger.debug("🧪 Testing Text Tools endpoint")
    return {"message": "Text Tools router is working"}

@router.post("/to_uppercase", summary="Convert text to uppercase")
async def to_uppercase(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting text to uppercase: %s", request.text)
    if not request.text:
        logger.error("❌ Text is required")
        raise HTTPException(status_code=400, detail="Text is required")
    try:
        uppercase = request.text.upper()
        logger.debug("✅ Text converted to uppercase: %s", uppercase)
        return {"text": request.text, "uppercase": uppercase}
    except Exception as e:
        logger.exception("💥 Error converting text to uppercase: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting text to uppercase: {str(e)}")