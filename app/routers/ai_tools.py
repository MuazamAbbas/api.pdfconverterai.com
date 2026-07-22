from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.core.security import verify_api_key
from app.core.config import settings
from app.services.ai_tools.sentiment import analyze_sentiment_service
import logging

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/ai_tools", tags=["AI Tools"])

class TextRequest(BaseModel):
    text: str

@router.get("/test", summary="Test AI Tools endpoint")
async def test_ai_tools(api_key: dict = Depends(verify_api_key)):
    logger.debug("🧪 Testing AI Tools endpoint")
    return {"message": "AI Tools router is working"}


@router.post("/sentiment", summary="Analyze sentiment of text")
async def sentiment_analysis(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Analyzing sentiment for text: %s", request.text)
    try:
        result = await analyze_sentiment_service(request.text)
        logger.debug("✅ Sentiment analysis completed: %s", result)
        return result
    except ValueError as e:
        logger.error("❌ Validation error: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error analyzing sentiment: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error analyzing sentiment: {str(e)}")