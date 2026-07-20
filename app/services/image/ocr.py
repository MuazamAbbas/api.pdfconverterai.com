import pytesseract
from PIL import Image, ImageEnhance
import io
import logging

# Explicitly set Tesseract path
pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"

logger = logging.getLogger(__name__)

async def extract_text_from_image(image_data: bytes) -> str:
    """
    Extract text from image data using Tesseract OCR.
    Args:
        image_data (bytes): Raw image data.
    Returns:
        str: Extracted text.
    Raises:
        ValueError: If no text is detected.
        Exception: For other errors.
    """
    try:
        # Open image
        image = Image.open(io.BytesIO(image_data)).convert("L")  # Grayscale
        
        # Preprocess: Enhance contrast and binarize
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(2.0)  # Increase contrast
        image = image.point(lambda p: p > 128 and 255)  # Threshold
        
        # Perform OCR
        text = pytesseract.image_to_string(
            image,
            lang="eng",
            config="--psm 6"  # Single block of text
        ).strip()
        
        if not text:
            logger.error("No text detected in image")
            raise ValueError("No text detected in image")
        
        return text
    except Exception as e:
        logger.exception("Error in OCR processing: %s", str(e))
        raise