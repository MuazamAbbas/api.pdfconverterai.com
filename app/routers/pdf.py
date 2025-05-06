from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
import os
import shutil
import PyPDF2
import pymupdf  # MuPDF for better text extraction
import logging
from app.core.security import verify_api_key
from app.services.pdf.pdf_to_word import convert_pdf_to_word

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pdf", tags=["PDF Tools"])

@router.get("/test", summary="Test PDF Tools endpoint")
async def test_pdf(api_key: dict = Depends(verify_api_key)):
    logger.debug("🧪 Testing PDF Tools endpoint")
    return {"message": "PDF Tools router is working"}

@router.post("/upload", summary="Upload a PDF file")
async def upload_pdf(file: UploadFile = File(...), api_key: dict = Depends(verify_api_key)):
    logger.debug("📤 Uploading PDF: %s", file.filename)
    if not file.filename.endswith(".pdf"):
        logger.error("❌ File is not a PDF: %s", file.filename)
        raise HTTPException(status_code=400, detail="File must be a PDF")
    os.makedirs("/tmp/pdfconverterai", exist_ok=True)
    file_path = f"/tmp/pdfconverterai/{file.filename}"
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logger.debug("✅ PDF uploaded to: %s", file_path)
        return {"filename": file.filename, "path": file_path}
    except Exception as e:
        logger.exception("💥 Error uploading PDF: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error uploading PDF: {str(e)}")
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.debug("🗑️ Removed temporary file: %s", file_path)
            except Exception as e:
                logger.error("❌ Failed to remove temporary file %s: %s", file_path, str(e))

@router.post("/convert", summary="Convert PDF to text")
async def convert_pdf(file: UploadFile = File(...), api_key: dict = Depends(verify_api_key)):
    logger.debug("📄 Converting PDF: %s", file.filename)
    if not file.filename.endswith(".pdf"):
        logger.error("❌ File is not a PDF: %s", file.filename)
        raise HTTPException(status_code=400, detail="File must be a PDF")
    os.makedirs("/tmp/pdfconverterai", exist_ok=True)
    file_path = f"/tmp/pdfconverterai/{file.filename}"
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logger.debug("✅ PDF saved to: %s", file_path)
        try:
            # Try pymupdf for text extraction
            doc = pymupdf.open(file_path)
            text = ""
            for page in doc:
                extracted_text = page.get_text("text")
                if extracted_text:
                    text += extracted_text + "\n"
            doc.close()
            if not text:
                # Fallback to PyPDF2
                pdf_reader = PyPDF2.PdfReader(file_path, strict=False)
                for page in pdf_reader.pages:
                    extracted_text = page.extract_text()
                    if extracted_text:
                        text += extracted_text + "\n"
            if not text:
                logger.error("❌ No text extracted from PDF: %s", file_path)
                raise HTTPException(status_code=400, detail="No text extracted from PDF")
            logger.debug("✅ Text extracted from PDF: %s", file_path)
            return {"filename": file.filename, "text": text.strip().replace("\n", "\t")}
        except Exception as e:
            logger.exception("💥 Error extracting text from PDF: %s", str(e))
            raise HTTPException(status_code=400, detail=f"Invalid PDF file: {str(e)}")
    except Exception as e:
        logger.exception("💥 Error processing PDF: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error processing PDF: {str(e)}")
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.debug("🗑️ Removed temporary file: %s", file_path)
            except Exception as e:
                logger.error("❌ Failed to remove temporary file %s: %s", file_path, str(e))

@router.post("/to_word", summary="Convert PDF to Word")
async def pdf_to_word(file: UploadFile = File(...), api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting PDF to Word: %s", file.filename)
    if not file.filename.endswith(".pdf"):
        logger.error("❌ Invalid file type: %s", file.filename)
        raise HTTPException(status_code=400, detail="File must be a PDF")
    try:
        output_path = await convert_pdf_to_word(file)
        logger.debug("✅ Conversion successful: %s", output_path)
        return {"filename": file.filename, "output_path": output_path}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("💥 Error converting PDF to Word: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting PDF to Word: {str(e)}")