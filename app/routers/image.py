from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from app.core.security import verify_api_key
from app.services.image.ocr import extract_text_from_image
import logging

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

@router.get("/test", summary="Test Image Tools endpoint")
async def test_image(api_key: dict = Depends(verify_api_key)):
    logger.debug("🧪 Testing Image Tools endpoint")
    return {"message": "Image Tools router is working"}

@router.post("/ocr", summary="Extract text from image using OCR")
async def ocr_image(file: UploadFile = File(...), api_key: dict = Depends(verify_api_key)):
    logger.debug("📸 Extracting text from image: %s", file.filename)
    if not file.content_type.startswith("image/"):
        logger.error("❌ File is not an image: %s", file.filename)
        raise HTTPException(status_code=400, detail="File must be an image")
    try:
        image_data = await file.read()
        text = await extract_text_from_image(image_data)
        logger.debug("✅ Text extracted from %s: %s", file.filename, text)
        return {"filename": file.filename, "text": text}
    except ValueError as e:
        logger.error("❌ Validation error: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error extracting text from image: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error extracting text from image: {str(e)}")