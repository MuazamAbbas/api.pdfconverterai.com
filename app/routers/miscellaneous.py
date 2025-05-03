from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime
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

router = APIRouter(prefix="/miscellaneous", tags=["miscellaneous"])

@router.get("/test", summary="Test Miscellaneous endpoint")
async def test_miscellaneous():
    logger.debug("🧪 Testing Miscellaneous endpoint")
    return {"message": "Miscellaneous router is working"}

@router.get("/timestamp", summary="Get current timestamp")
async def get_timestamp(api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Generating timestamp")
    try:
        timestamp = int(datetime.utcnow().timestamp())
        logger.debug("✅ Timestamp generated: %d", timestamp)
        return {"timestamp": timestamp}
    except Exception as e:
        logger.exception("💥 Error generating timestamp: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error generating timestamp: {str(e)}")