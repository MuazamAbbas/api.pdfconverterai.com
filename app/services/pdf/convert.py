"""Text extraction from a PDF already saved on disk.

Extracted out of `app/routers/pdf.py` (was inline in the old synchronous
`convert_pdf` handler) so it's a reusable, testable service the
`pdf_convert` Tier 2 job (`app/services/pdf/processors.py`,
`app/worker.py`) can call - matching the existing
`pdf_to_word.py`/`summarize.py` service-file pattern (Handbook Part C.3).
"""
import logging

import PyPDF2
import pymupdf  # MuPDF for better text extraction

logger = logging.getLogger(__name__)


async def extract_text_from_pdf(file_path: str) -> str:
    """Extract text from a PDF, trying PyMuPDF first and falling back to PyPDF2.

    Raises:
        ValueError: if the PDF can't be read or has no extractable text -
            treated as a permanent, non-retryable failure by the caller
            (ADR-003: corrupted/unsupported input never retries).
    """
    text = ""
    try:
        doc = pymupdf.open(file_path)
        for page in doc:
            extracted = page.get_text("text")
            if extracted:
                text += extracted + "\n"
        doc.close()
    except Exception as e:
        logger.warning("PyMuPDF extraction failed for %s: %s", file_path, str(e))

    if not text:
        try:
            reader = PyPDF2.PdfReader(file_path, strict=False)
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted + "\n"
        except Exception as e:
            logger.error("PyPDF2 fallback extraction failed for %s: %s", file_path, str(e))
            raise ValueError("Invalid or unreadable PDF file") from e

    if not text:
        logger.warning("No text extracted from PDF: %s", file_path)
        raise ValueError("No text could be extracted from this PDF")

    return text.strip()
