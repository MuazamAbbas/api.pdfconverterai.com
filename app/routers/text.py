from fastapi import APIRouter, Depends, HTTPException, FastAPI
from app.models.text import TextRequest, TextResponse, SentimentResponse, GrammarResponse
from app.core.security import verify_api_key
from app.core.config import settings
from app.services.text.sentiment import SentimentAnalyzer
from app.services.text.paraphrase import Paraphraser
from app.services.text.summarize import Summarizer
from app.services.text.grammar import correct_grammar
from app.services.text.count import word_count, char_count, sentence_count, paragraph_count
import logging
import json

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/text", tags=["Text Tools"])

def get_app() -> FastAPI:
    from app.main import app
    return app

@router.get("/test", summary="Test Text Tools endpoint", response_model=TextResponse)
async def test_text(api_key: dict = Depends(verify_api_key)):
    logger.debug("🧪 Testing Text Tools endpoint")
    try:
        response = TextResponse(result={"message": "Text Tools router is working"})
        logger.debug("✅ Test endpoint response: %s", json.dumps(response.dict()))
        return response
    except Exception as e:
        logger.exception("💥 Error in test endpoint: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error in test endpoint: {str(e)}")

@router.post(
    "/paraphrase",
    summary="Paraphrase text using AI",
    response_model=TextResponse,
    responses={
        200: {"description": "Paraphrased text returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def paraphrase(request: TextRequest, app: FastAPI = Depends(get_app), _ = Depends(verify_api_key)):
    logger.debug("🔧 Paraphrasing text: %s", request.text)
    try:
        paraphraser = Paraphraser(app)
        paraphrased = await paraphraser.paraphrase(request.text)
        logger.debug("✅ Paraphrased text: %s", paraphrased)
        return TextResponse(text=request.text, result={"paraphrased": paraphrased})
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error paraphrasing text: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error paraphrasing text: {str(e)}")

@router.post(
    "/summarize",
    summary="Summarize text using AI",
    response_model=TextResponse,
    responses={
        200: {"description": "Summarized text returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def summarize(request: TextRequest, app: FastAPI = Depends(get_app), _ = Depends(verify_api_key)):
    logger.debug("📝 Summarizing text: %s", request.text)
    try:
        summarizer = Summarizer(app)
        summary = await summarizer.summarize_text(request.text)
        logger.debug("✅ Summarized text: %s", summary)
        return TextResponse(text=request.text, result={"summary": summary})
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error summarizing text: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error summarizing text: {str(e)}")

@router.post(
    "/grammar",
    summary="Correct grammar in text",
    response_model=GrammarResponse,
    responses={
        200: {"description": "Grammar corrections returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def grammar(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Correcting grammar for text: %s", request.text)
    try:
        result = await correct_grammar(request.text)
        logger.debug("✅ Grammar corrections: %s", result["corrections"])
        return GrammarResponse(**result)
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error correcting grammar: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error correcting grammar: {str(e)}")

@router.post(
    "/word_count",
    summary="Count words in text",
    response_model=TextResponse,
    responses={
        200: {"description": "Word count returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def word_count_endpoint(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔢 Counting words in text: %s", request.text)
    try:
        count = await word_count(request.text)
        logger.debug("✅ Word count: %d", count)
        return TextResponse(text=request.text, result={"word_count": count})
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error counting words: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error counting words: {str(e)}")

@router.post(
    "/char_count",
    summary="Count characters in text",
    response_model=TextResponse,
    responses={
        200: {"description": "Character count returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def char_count_endpoint(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔢 Counting characters in text: %s", request.text)
    try:
        count = await char_count(request.text)
        logger.debug("✅ Character count: %d", count)
        return TextResponse(text=request.text, result={"char_count": count})
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error counting characters: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error counting characters: {str(e)}")

@router.post(
    "/sentence_count",
    summary="Count sentences in text",
    response_model=TextResponse,
    responses={
        200: {"description": "Sentence count returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def sentence_count_endpoint(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔢 Counting sentences in text: %s", request.text)
    try:
        count = await sentence_count(request.text)
        logger.debug("✅ Sentence count: %d", count)
        return TextResponse(text=request.text, result={"sentence_count": count})
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error counting sentences: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error counting sentences: {str(e)}")

@router.post(
    "/paragraph_count",
    summary="Count paragraphs in text",
    response_model=TextResponse,
    responses={
        200: {"description": "Paragraph count returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def paragraph_count_endpoint(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔢 Counting paragraphs in text: %s", request.text)
    try:
        count = await paragraph_count(request.text)
        logger.debug("✅ Paragraph count: %d", count)
        return TextResponse(text=request.text, result={"paragraph_count": count})
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error counting paragraphs: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error counting paragraphs: {str(e)}")

@router.post(
    "/sentiment",
    summary="Analyze sentiment of text using TextBlob",
    response_model=SentimentResponse,
    responses={
        200: {"description": "Sentiment analysis returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def sentiment(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("😊 Analyzing sentiment for text: %s", request.text)
    try:
        analyzer = SentimentAnalyzer()
        result = await analyzer.analyze(request.text)
        logger.debug("✅ Sentiment analysis: %s", result)
        return SentimentResponse(text=request.text, **result)
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error analyzing sentiment: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error analyzing sentiment: {str(e)}")