"""Concrete Processor for the `image` module's single Tier 2 job,
`image_ocr` (Handbook Part C.4, ADR-003). Mirrors
`app/services/pdf/processors.py` - implements the shared Validate/Prepare/
Execute/Verify/Cleanup interface from `app/services/jobs/processor.py`;
`app/worker.py`'s `image_ocr` task function is the thin ARQ-facing wrapper
that calls `.run()` and owns the Job's Pending/Queued/Processing/Completed/
Failed transitions.

`image` depends on `jobs` here (imports its Processor base), matching the
Handbook Part C.3 one-direction dependency chain (... file -> job ->
pdf/image/ai) - `jobs` never imports anything from `image`.
"""
import logging
import os

from app.services.files.service import IMAGE_ALLOWED_EXTENSIONS
from app.services.image.ocr import extract_text_from_image
from app.services.jobs.processor import (
    PermanentProcessingError,
    Processor,
    TransientProcessingError,
)

logger = logging.getLogger(__name__)


def _validate_image_input(file_doc) -> None:
    ext = os.path.splitext(file_doc.originalFilename)[1].lower()
    if ext not in IMAGE_ALLOWED_EXTENSIONS:
        raise PermanentProcessingError("File must be a supported image (.jpg, .jpeg, .png)")
    if not os.path.exists(file_doc.storagePath):
        raise PermanentProcessingError("Source file is missing or has expired")


class ImageOcrProcessor(Processor):
    """job.type == "image_ocr" """

    async def validate(self, job, file_doc):
        _validate_image_input(file_doc)

    async def prepare(self, job, file_doc):
        return {"path": file_doc.storagePath}

    async def execute(self, job, file_doc, prepared, ctx=None):
        try:
            with open(prepared["path"], "rb") as f:
                image_data = f.read()
            text = await extract_text_from_image(image_data)
        except ValueError as e:
            raise PermanentProcessingError(str(e)) from e
        except Exception as e:
            raise TransientProcessingError("Temporary error extracting text from the image") from e
        return {"text": text}

    async def verify(self, job, file_doc, result):
        if not result.get("text"):
            raise PermanentProcessingError("No text could be extracted from this image")
