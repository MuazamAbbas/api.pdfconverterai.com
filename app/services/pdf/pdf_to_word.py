import logging
import os

from pdf2docx import Converter

logger = logging.getLogger(__name__)


async def convert_pdf_to_word(pdf_path: str, output_dir: str) -> str:
    """Convert a PDF already on disk to a .docx file in `output_dir`.

    Returns the output path. Callers (the `pdf_to_word` Tier 2 job,
    `app/services/pdf/processors.py`) are responsible for turning exceptions
    into the ADR-003 retry classification - a `ValueError` here means the
    input itself is bad (permanent, no retry); anything else is treated as
    a transient conversion failure by the caller.

    Note: this used to accept a FastAPI `UploadFile` and save it to disk
    itself; it now takes a path because the file is already on disk and
    tracked in Mongo (`files` collection) by the time a job runs it -
    see Handbook Part C.4/C.9 and the files/jobs lifecycle this pairs with.
    """
    if not os.path.exists(pdf_path):
        raise ValueError("Source PDF not found")

    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    output_path = os.path.join(output_dir, f"{base_name}-{os.getpid()}.docx")
    cv = Converter(pdf_path)
    try:
        cv.convert(output_path)
    finally:
        cv.close()
    logger.debug("Converted PDF to Word: %s", output_path)
    return output_path
