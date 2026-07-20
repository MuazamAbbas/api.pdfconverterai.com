from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import logging
from app.core.security import verify_api_key
from app.services.seo.keyword_density import keyword_density
from app.services.seo.keyword_extract import extract_keywords_service

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/seo_tools", tags=["SEO Tools"])

class KeywordDensityRequest(BaseModel):
    text: str

class KeywordExtractRequest(BaseModel):
    text: str

@router.post("/keyword_density", summary="Calculate keyword density")
async def calculate_keyword_density(request: KeywordDensityRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Calculating keyword density for text: %s", request.text)
    try:
        result = await keyword_density(request.text)
        logger.debug("✅ Keyword density result: %s", result)
        return result
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error calculating keyword density: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error calculating keyword density: {str(e)}")

@router.post("/keyword_extract", summary="Extract keywords from text")
async def keyword_extract(request: KeywordExtractRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Extracting keywords for text: %s", request.text)
    try:
        keywords = await extract_keywords_service(request.text)
        logger.debug("✅ Keywords extracted: %s", keywords)
        return {"text": request.text, "keywords": keywords}
    except ValueError as e:
        logger.error("❌ Validation error: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error extracting keywords: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error extracting keywords: {str(e)}")