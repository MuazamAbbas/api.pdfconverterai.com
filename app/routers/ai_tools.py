import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.security import verify_api_key
from app.services.ai.grammar_checker import GrammarCheckerUnavailableError, check_grammar
from app.services.ai_tools.sentiment import analyze_sentiment_service
from app.shared.responses import api_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai_tools", tags=["AI Tools"])

class TextRequest(BaseModel):
    text: str


class GrammarCheckerRequest(BaseModel):
    text: str
    language: str = "en-US"

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
        raise api_error(500, "Failed to analyze sentiment", "SENTIMENT_ANALYSIS_FAILED")


@router.post("/grammar_checker", summary="Check grammar via the LanguageTool API")
async def grammar_checker(request: GrammarCheckerRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug(
        "🔍 Checking grammar for text (%d chars, language=%s)",
        len(request.text or ""),
        request.language,
    )
    try:
        result = await check_grammar(request.text, request.language)
        logger.debug("✅ Grammar check completed: %d issue(s)", len(result["issues"]))
        return result
    except ValueError as e:
        logger.error("❌ Validation error: %s", str(e))
        raise api_error(400, str(e), "GRAMMAR_CHECK_INVALID_INPUT")
    except GrammarCheckerUnavailableError as e:
        logger.error("🚫 LanguageTool API unavailable: %s", str(e))
        raise api_error(
            503,
            "Grammar checking is temporarily unavailable, please try again shortly",
            "GRAMMAR_CHECK_UNAVAILABLE",
        )
    except Exception as e:
        logger.exception("💥 Error checking grammar: %s", str(e))
        raise api_error(500, "Failed to check grammar", "GRAMMAR_CHECK_FAILED")