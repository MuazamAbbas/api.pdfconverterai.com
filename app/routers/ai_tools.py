from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
import logging
from app.core.security import verify_api_key

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai_tools", tags=["ai_tools"])

class SummarizeRequest(BaseModel):
    text: str

@router.get("/test", summary="Test AI Tools endpoint")
async def test_ai_tools():
    logger.debug("🧪 Testing AI Tools endpoint")
    return {"message": "AI Tools router is working"}

@router.post("/summarize", summary="Summarize text")
async def summarize_text(request: SummarizeRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("📝 Summarizing text, length: %d", len(request.text))
    if len(request.text) < 50:
        logger.error("❌ Text too short: %d characters", len(request.text))
        raise HTTPException(status_code=400, detail="Text must be at least 50 characters")
    try:
        # Simple summarization: take first 30% of sentences
        sentences = request.text.split(". ")
        summary_length = max(1, len(sentences) // 3)
        summary = ". ".join(sentences[:summary_length]) + "."
        logger.debug("✅ Summary generated, length: %d", len(summary))
        return {"text": request.text, "summary": summary}
    except Exception as e:
        logger.exception("💥 Error summarizing text: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error summarizing text: {str(e)}")