from fastapi import APIRouter, HTTPException, Depends
from pydantic import HttpUrl
import logging
from app.core.security import verify_api_key
from app.core.config import settings
from app.services.video.youtube_metadata import fetch_youtube_metadata

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/video", tags=["video"])

@router.get("/test", summary="Test Video Tools endpoint")
async def test_video():
    logger.debug("🧪 Testing Video Tools endpoint")
    return {"message": "Video Tools router is working"}

@router.get("/placeholder", summary="Placeholder for video processing")
async def placeholder_video(api_key: dict = Depends(verify_api_key)):
    logger.debug("📹 Video processing placeholder accessed")
    return {"message": "Video processing tools coming soon"}

@router.post("/youtube_metadata", summary="Fetch YouTube video metadata")
async def youtube_metadata(url: HttpUrl, api_key: dict = Depends(verify_api_key)):
    """
    Fetch metadata (title, description, duration) from a YouTube video URL.
    Args:
        url: YouTube video URL.
    Returns:
        dict: Metadata including title, description, duration.
    """
    logger.debug("📹 Fetching YouTube metadata for URL: %s", url)
    try:
        metadata = await fetch_youtube_metadata(url)
        logger.debug("✅ Metadata fetched: %s", metadata)
        return metadata
    except ValueError as e:
        logger.error("Error fetching YouTube metadata: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Unexpected error in youtube_metadata: %s", str(e))
        raise HTTPException(status_code=500, detail="Internal server error")