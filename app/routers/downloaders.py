from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import yt_dlp
import os
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

router = APIRouter(prefix="/downloaders", tags=["Downloaders"])

class YouTubeRequest(BaseModel):
    url: str

@router.get("/test", summary="Test Downloaders endpoint")
async def test_downloaders():
    logger.debug("🧪 Testing Downloaders endpoint")
    return {"message": "Downloaders router is working"}

@router.post("/youtube", summary="Download YouTube video")
async def download_youtube(request: YouTubeRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("📥 Downloading YouTube video: %s", request.url)
    try:
        output_path = "/tmp/pdfconverterai"
        os.makedirs(output_path, exist_ok=True)
        ydl_opts = {
            "outtmpl": f"{output_path}/%(id)s.%(ext)s",
            "format": "best",
            "cookiefile": "/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/app/config/cookies.txt",
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(request.url, download=True)
            filename = ydl.prepare_filename(info)
            file_url = f"https://api.pdfconverterai.com/files/{os.path.basename(filename)}"
        logger.debug("✅ Video downloaded: %s", filename)
        return {"url": request.url, "file_url": file_url}
    except Exception as e:
        logger.exception("💥 Error downloading video: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error downloading video: {str(e)}")