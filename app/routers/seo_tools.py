import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import verify_api_key
from app.services.seo.content_readability_analyzer import analyze_readability
from app.services.seo.keyword_density import keyword_density
from app.services.seo.keyword_extract import extract_keywords_service
from app.services.seo.meta_tag_generator import generate_meta_tags
from app.services.seo.serp_preview import analyze_serp_preview
from app.services.seo.social_media_tags_generator import generate_social_media_tags
from app.services.seo.title_length_checker import check_title_length
from app.services.seo.url_slug_generator import generate_slug
from app.shared.responses import api_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/seo_tools", tags=["SEO Tools"])

class KeywordDensityRequest(BaseModel):
    text: str

class KeywordExtractRequest(BaseModel):
    text: str

class TitleLengthCheckerRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)

class MetaTagGeneratorRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: str = Field(..., min_length=1, max_length=2000)
    keywords: Optional[str] = Field(default=None, max_length=1000)

class UrlSlugGeneratorRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)

class ContentReadabilityAnalyzerRequest(BaseModel):
    text: str = Field(..., min_length=20, max_length=10000)

class SocialMediaTagsGeneratorRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: str = Field(..., min_length=1, max_length=2000)
    image_url: str = Field(..., min_length=1, max_length=2000)
    url: Optional[str] = Field(default=None, max_length=2000)

class SerpPreviewRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: str = Field(..., min_length=1, max_length=2000)
    url: str = Field(..., min_length=1, max_length=2000)

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
        raise api_error(500, "Failed to calculate keyword density", "SEO_ANALYSIS_FAILED")

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
        raise api_error(500, "Failed to extract keywords", "SEO_ANALYSIS_FAILED")

@router.post("/title_length_checker", summary="Check a page title's length against SERP guidance")
async def title_length_checker(request: TitleLengthCheckerRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Checking title length for: %s", request.title)
    try:
        result = await check_title_length(request.title)
        logger.debug("✅ Title length check result: %s", result)
        return result
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error checking title length: %s", str(e))
        raise api_error(500, "Failed to check title length", "TITLE_LENGTH_CHECK_FAILED")

@router.post("/meta_tag_generator", summary="Generate HTML meta tags from title/description/keywords")
async def meta_tag_generator(request: MetaTagGeneratorRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Generating meta tags for title: %s", request.title)
    try:
        result = await generate_meta_tags(request.title, request.description, request.keywords)
        logger.debug("✅ Meta tags generated")
        return result
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error generating meta tags: %s", str(e))
        raise api_error(500, "Failed to generate meta tags", "META_TAG_GENERATION_FAILED")

@router.post("/url_slug_generator", summary="Generate a URL-safe slug from text")
async def url_slug_generator(request: UrlSlugGeneratorRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Generating slug for text: %s", request.text)
    try:
        result = await generate_slug(request.text)
        logger.debug("✅ Slug generated: %s", result)
        return result
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error generating slug: %s", str(e))
        raise api_error(500, "Failed to generate slug", "SLUG_GENERATION_FAILED")

@router.post("/content_readability_analyzer", summary="Analyze text readability (Flesch scores)")
async def content_readability_analyzer(request: ContentReadabilityAnalyzerRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Analyzing readability for text of length: %d", len(request.text))
    try:
        result = await analyze_readability(request.text)
        logger.debug("✅ Readability analysis result: %s", result)
        return result
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error analyzing readability: %s", str(e))
        raise api_error(500, "Failed to analyze readability", "READABILITY_ANALYSIS_FAILED")

@router.post("/social_media_tags_generator", summary="Generate Open Graph and Twitter Card meta tags")
async def social_media_tags_generator(request: SocialMediaTagsGeneratorRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Generating social media tags for title: %s", request.title)
    try:
        result = await generate_social_media_tags(
            request.title, request.description, request.image_url, request.url
        )
        logger.debug("✅ Social media tags generated")
        return result
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error generating social media tags: %s", str(e))
        raise api_error(500, "Failed to generate social media tags", "SOCIAL_TAGS_GENERATION_FAILED")

@router.post("/serp_preview", summary="Get SERP truncation metadata for a title/description/url")
async def serp_preview(request: SerpPreviewRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Building SERP preview for url: %s", request.url)
    try:
        result = await analyze_serp_preview(request.title, request.description, request.url)
        logger.debug("✅ SERP preview result: %s", result)
        return result
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error building SERP preview: %s", str(e))
        raise api_error(500, "Failed to build SERP preview", "SERP_PREVIEW_FAILED")