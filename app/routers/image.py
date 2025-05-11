from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from transformers import pipeline
from PIL import Image
import io
import logging
from app.core.security import verify_api_key
from app.services.image.caption import caption_image_service

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/image", tags=["Image Tools"])
captioner = pipeline("image-to-text", model="Salesforce/blip-image-captioning-base")

@router.get("/test", summary="Test Image Tools endpoint")
async def test_image(api_key: dict = Depends(verify_api_key)):
    logger.debug("🧪 Testing Image Tools endpoint")
    return {"message": "Image Tools router is working"}

@router.get("/placeholder", summary="Placeholder for image processing")
async def placeholder_image(api_key: dict = Depends(verify_api_key)):
    logger.debug("📸 Image processing placeholder accessed")
    return {"message": "Image processing tools coming soon"}

@router.post("/caption", summary="Generate caption for image")
async def caption_image(file: UploadFile = File(...), api_key: dict = Depends(verify_api_key)):
    logger.debug("📸 Captioning image: %s", file.filename)
    if not file.content_type.startswith("image/"):
        logger.error("❌ File is not an image: %s", file.filename)
        raise HTTPException(status_code=400, detail="File must be an image")
    try:
        image_data = await file.read()
        caption = await caption_image_service(image_data)
        logger.debug("✅ Caption generated for: %s", file.filename)
        return {"filename": file.filename, "caption": caption}
    except ValueError as e:
        logger.error("❌ Validation error: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error captioning image: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error captioning image: {str(e)}")