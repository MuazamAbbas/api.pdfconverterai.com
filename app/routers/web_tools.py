from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import urllib.parse
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

router = APIRouter(prefix="/web_tools", tags=["web_tools"])

class URLEncodeRequest(BaseModel):
    url: str

@router.get("/test", summary="Test Web Tools endpoint")
async def test_web_tools():
    logger.debug("🧪 Testing Web Tools endpoint")
    return {"message": "Web Tools router is working"}

@router.post("/url_encode", summary="Encode a URL")
async def url_encode(request: URLEncodeRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Encoding URL: %s", request.url)
    try:
        encoded_url = urllib.parse.quote(request.url)
        logger.debug("✅ URL encoded: %s", encoded_url)
        return {"original_url": request.url, "encoded_url": encoded_url}
    except Exception as e:
        logger.exception("💥 Error encoding URL: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error encoding URL: {str(e)}")