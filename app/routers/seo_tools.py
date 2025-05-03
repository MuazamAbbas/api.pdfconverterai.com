from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from nltk.tokenize import word_tokenize
from collections import Counter
import re
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

router = APIRouter(prefix="/seo_tools", tags=["seo_tools"])

class KeywordDensityRequest(BaseModel):
    text: str

@router.get("/test", summary="Test SEO Tools endpoint")
async def test_seo_tools():
    logger.debug("🧪 Testing SEO Tools endpoint")
    return {"message": "SEO Tools router is working"}

@router.post("/keyword_density", summary="Analyze keyword density in text")
async def keyword_density(request: KeywordDensityRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Analyzing keyword density for text, length: %d", len(request.text))
    if len(request.text) < 50:
        logger.error("❌ Text too short: %d characters", len(request.text))
        raise HTTPException(status_code=400, detail="Text must be at least 50 characters")
    try:
        words = word_tokenize(request.text.lower())
        words = [word for word in words if re.match(r'^\w+$', word)]
        total_words = len(words)
        if total_words == 0:
            logger.error("❌ No valid words found in text")
            raise HTTPException(status_code=400, detail="No valid words found")
        word_counts = Counter(words)
        density = {
            word: {"count": count, "density": round((count / total_words) * 100, 2)}
            for word, count in word_counts.items()
        }
        logger.debug("✅ Keyword density analyzed, total words: %d", total_words)
        return {"text_length": total_words, "keyword_density": density}
    except Exception as e:
        logger.exception("💥 Error analyzing keyword density: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error analyzing text: {str(e)}")