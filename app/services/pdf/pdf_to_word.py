import logging
import os
import shutil
from pdf2docx import Converter
from fastapi import UploadFile, HTTPException

logger = logging.getLogger(__name__)

async def convert_pdf_to_word(pdf_file: UploadFile) -> str:
    """Convert PDF to Word and return file path."""
    if not pdf_file.filename.endswith(".pdf"):
        logger.error("❌ Invalid file type: %s", pdf_file.filename)
        raise HTTPException(status_code=400, detail="File must be a PDF")
    os.makedirs("/tmp/pdfconverterai", exist_ok=True)
    temp_pdf_path = f"/tmp/pdfconverterai/{pdf_file.filename}"
    output_path = f"/tmp/pdfconverterai/{pdf_file.filename}.docx"
    try:
        # Save uploaded file
        with open(temp_pdf_path, "wb") as buffer:
            shutil.copyfileobj(pdf_file.file, buffer)
        logger.debug("✅ PDF saved to: %s", temp_pdf_path)
        # Convert to Word
        cv = Converter(temp_pdf_path)
        cv.convert(output_path)
        cv.close()
        logger.debug("✅ Converted PDF to Word: %s", output_path)
        return output_path
    except Exception as e:
        logger.exception("💥 Error converting PDF to Word: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting PDF to Word: {str(e)}")
    finally:
        if os.path.exists(temp_pdf_path):
            try:
                os.remove(temp_pdf_path)
                logger.debug("🗑️ Removed temporary file: %s", temp_pdf_path)
            except Exception as e:
                logger.error("❌ Failed to remove temporary file %s: %s", temp_pdf_path, str(e))