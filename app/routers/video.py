from fastapi import APIRouter, HTTPException, Depends
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

router = APIRouter(prefix="/video", tags=["video"])

@router.get("/test", summary="Test Video Tools endpoint")
async def test_video():
    logger.debug("🧪 Testing Video Tools endpoint")
    return {"message": "Video Tools router is working"}

@router.get("/placeholder", summary="Placeholder for video processing")
async def placeholder_video(api_key: dict = Depends(verify_api_key)):
    logger.debug("📹 Video processing placeholder accessed")
    return {"message": "Video processing tools coming soon"}