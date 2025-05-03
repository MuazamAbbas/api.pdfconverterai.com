from fastapi import APIRouter, Depends
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

router = APIRouter(prefix="/image", tags=["image"])

@router.get("/test", summary="Test Image Tools endpoint")
async def test_image():
    logger.debug("🧪 Testing Image Tools endpoint")
    return {"message": "Image Tools router is working"}

@router.get("/placeholder", summary="Placeholder for image processing")
async def placeholder_image(api_key: dict = Depends(verify_api_key)):
    logger.debug("📸 Image processing placeholder accessed")
    return {"message": "Image processing tools coming soon"}