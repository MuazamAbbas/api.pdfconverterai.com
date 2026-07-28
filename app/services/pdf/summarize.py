import pymupdf
import logging

logger = logging.getLogger(__name__)


async def summarize_pdf_service(file_path: str, summarizer) -> str:
    """
    Summarize text extracted from a PDF file.
    Args:
        file_path (str): Path to the PDF file.
        summarizer: Preloaded Hugging Face summarization pipeline
            (`facebook/bart-large-cnn`), loaded once per worker process in
            `app/worker.py`'s `on_startup` hook and passed in via
            `ctx["summarizer_pipeline"]` (Handbook Part C.2/C.4, ADR-003/
            ADR-006) - this module no longer loads its own model.
    Returns:
        str: Summarized text.
    Raises:
        ValueError: If no text is extracted or text is too short.
    """
    try:
        doc = pymupdf.open(file_path)
        text = ""
        for page in doc:
            extracted_text = page.get_text("text")
            if extracted_text:
                text += extracted_text + "\n"
        doc.close()
        if not text:
            logger.error("No text extracted from PDF: %s", file_path)
            raise ValueError("No text extracted from PDF")
        if len(text) < 50:
            logger.error("Text too short: %d characters", len(text))
            raise ValueError("Text must be at least 50 characters")
        summary = summarizer(text, max_length=150, min_length=30, do_sample=False)[0]["summary_text"]
        logger.debug("Summary generated, length: %d", len(summary))
        return summary
    except Exception as e:
        logger.exception("Error summarizing PDF: %s", str(e))
        raise
