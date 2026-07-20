from fastapi import APIRouter, HTTPException, Depends, FastAPI
from pydantic import BaseModel
import urllib.parse
import re
import aiohttp
import logging
from app.core.security import verify_api_key
from app.models.web_tools import URLEncodeRequest, WebpageSummarizeRequest
from app.services.web_tools.summarize import Summarizer
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web_tools", tags=["Web Tools"])

def get_app() -> FastAPI:
    from app.main import app
    return app

class URLRequest(BaseModel):
    url: str

@router.get("/test", summary="Test Web Tools endpoint")
async def test_web_tools(api_key: dict = Depends(verify_api_key)):
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

@router.post("/summarize", summary="Summarize webpage content")
async def webpage_summarize(request: WebpageSummarizeRequest, app: FastAPI = Depends(get_app), _ = Depends(verify_api_key)):
    logger.debug("📝 Summarizing webpage: %s", request.url)
    try:
        summarizer = Summarizer(app)
        summary = await summarizer.summarize_webpage(request.url)
        logger.debug("✅ Summary generated for: %s", request.url)
        return {"url": request.url, "summary": summary}
    except ValueError as e:
        logger.error("❌ Validation error: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error summarizing webpage: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error summarizing webpage: {str(e)}")

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(aiohttp.ClientResponseError),
    before_sleep=lambda retry_state: logger.debug("Retrying URL validation: attempt %d", retry_state.attempt_number)
)
async def check_url(session: aiohttp.ClientSession, url: str) -> tuple[bool, int]:
    async with session.get(url, allow_redirects=True, timeout=5) as response:
        status = response.status
        if 200 <= status < 400:
            logger.debug("✅ URL is reachable: %s, status: %d", url, status)
            return True, status
        elif status == 429:
            logger.warning("⚠️ Rate limit hit for URL: %s, status: %d", url, status)
            raise aiohttp.ClientResponseError(
                request_info=response.request_info,
                history=response.history,
                status=status,
                message="Too Many Requests"
            )
        else:
            logger.error("❌ URL is not reachable: %s, status: %d", url, status)
            return False, status

@router.post("/validate_url", summary="Validate URL and check if it is reachable")
async def validate_url(request: URLRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Validating URL: %s", request.url)
    url_pattern = re.compile(
        r"^(https?://)?([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(/[\w\-\./?%&=]*)?$",
        re.IGNORECASE
    )
    if not request.url:
        logger.error("❌ URL is required")
        raise HTTPException(status_code=400, detail="URL is required")
    if not url_pattern.match(request.url):
        logger.error("❌ Invalid URL format: %s", request.url)
        raise HTTPException(status_code=400, detail="Invalid URL format")
    try:
        async with aiohttp.ClientSession() as session:
            is_valid, status = await check_url(session, request.url)
            return {"url": request.url, "is_valid": is_valid, "status_code": status}
    except aiohttp.ClientResponseError as e:
        logger.exception("💥 Client response error validating URL: %s", str(e))
        return {"url": request.url, "is_valid": False, "status_code": e.status, "error": str(e)}
    except aiohttp.ClientError as e:
        logger.exception("💥 Client error validating URL: %s", str(e))
        return {"url": request.url, "is_valid": False, "status_code": None, "error": str(e)}
    except Exception as e:
        logger.exception("💥 Error validating URL: %s", str(e))
        return {"url": request.url, "is_valid": False, "status_code": None, "error": str(e)}