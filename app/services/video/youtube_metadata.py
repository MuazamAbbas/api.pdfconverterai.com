import yt_dlp
import logging
from typing import Dict

logger = logging.getLogger(__name__)

async def fetch_youtube_metadata(url: str) -> Dict:
    """
    Fetch metadata (title, description, duration) from a YouTube video URL.
    Args:
        url (str): YouTube video URL.
    Returns:
        dict: Metadata including title, description, duration.
    Raises:
        ValueError: If URL is invalid or metadata cannot be fetched.
    """
    if not url.startswith(("http://", "https://")) or "youtube.com" not in url:
        logger.error("Invalid YouTube URL: %s", url)
        raise ValueError("URL must be a valid YouTube URL")
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            metadata = {
                "title": info.get("title", ""),
                "description": info.get("description", "")[:500],  # Truncate description
                "duration_seconds": info.get("duration", 0),
                "url": url
            }
        logger.debug("Fetched metadata: %s", metadata)
        return metadata
    except Exception as e:
        logger.exception("Error fetching YouTube metadata: %s", str(e))
        raise ValueError(f"Error fetching metadata: {str(e)}")