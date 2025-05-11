from transformers import pipeline
from PIL import Image
import io
import logging

logger = logging.getLogger(__name__)
captioner = pipeline("image-to-text", model="Salesforce/blip-image-captioning-base")

async def caption_image_service(image_data: bytes) -> str:
    """
    Generate caption for an image.
    Args:
        image_data (bytes): Raw image data.
    Returns:
        str: Generated caption.
    Raises:
        ValueError: If image is invalid.
    """
    try:
        image = Image.open(io.BytesIO(image_data))
        caption = captioner(image)[0]["generated_text"]
        logger.debug("Caption generated: %s", caption)
        return caption
    except Exception as e:
        logger.exception("Error captioning image: %s", str(e))
        raise ValueError(f"Invalid image: {str(e)}")