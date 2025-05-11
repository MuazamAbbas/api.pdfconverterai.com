from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from transformers import pipeline
import logging
from app.core.security import verify_api_key
from app.services.ai_tools.sentiment import analyze_sentiment_service

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai_tools", tags=["AI Tools"])
sentiment_analyzer = pipeline("sentiment-analysis", model="distilbert-base-uncased-finetuned-sst-2-english")

class SummarizeRequest(BaseModel):
    text: str

class SentimentRequest(BaseModel):
    text: str

@router.get("/test", summary="Test AI Tools endpoint")
async def test_ai_tools(api_key: dict = Depends(verify_api_key)):
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

@router.post("/sentiment", summary="Analyze sentiment of text")
async def sentiment_analysis(request: SentimentRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Analyzing sentiment for text: %s", request.text)
    try:
        result = await analyze_sentiment_service(request.text)
        logger.debug("✅ Sentiment analysis completed")
        return result
    except ValueError as e:
        logger.error("❌ Validation error: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error analyzing sentiment: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error analyzing sentiment: {str(e)}")