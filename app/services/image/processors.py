"""Concrete Processors for the `image` module's Tier 2 jobs, `image_ocr`
and `image_compress` (Handbook Part C.4, ADR-003). Mirrors
`app/services/pdf/processors.py` - implements the shared Validate/Prepare/
Execute/Verify/Cleanup interface from `app/services/jobs/processor.py`;
`app/worker.py`'s `image_ocr`/`image_compress` task functions are the thin
ARQ-facing wrappers that call `.run()` and own the Job's Pending/Queued/
Processing/Completed/Failed transitions.

`image` depends on `jobs` here (imports its Processor base), matching the
Handbook Part C.3 one-direction dependency chain (... file -> job ->
pdf/image/ai) - `jobs` never imports anything from `image`.

`ImageCompressProcessor` (job.type == "image_compress") reads its `level`
input off `job.params["level"]` (one of "low"/"medium"/"high", validated at
the router - see `app/routers/image.py`'s `/image/compress`) the same way
`SplitProcessor` reads `ranges` off `job.params["ranges"]`
(`app/services/pdf/processors.py`) - generic `Job.params` field rather than
a new `Processor` method parameter.
"""
import logging
import os

from PIL import Image, UnidentifiedImageError

from app.core.storage import STORAGE_PATH
from app.services.files.service import IMAGE_ALLOWED_EXTENSIONS
from app.services.image.ocr import extract_text_from_image
from app.services.jobs.processor import (
    PermanentProcessingError,
    Processor,
    TransientProcessingError,
)

logger = logging.getLogger(__name__)

# Quality mapping for JPEG output (Handbook Part I.2 spec for this tool) -
# lower `quality` = smaller output/more compression. PNG is never
# force-converted to JPEG (it stays PNG, re-encoded with `optimize=True`
# only), so this mapping only applies to .jpg/.jpeg input.
_JPEG_QUALITY_BY_LEVEL = {"low": 85, "medium": 65, "high": 35}
DEFAULT_COMPRESSION_LEVEL = "medium"


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
            # Fixed, hardcoded message rather than `str(e)` - decouples this
            # client-facing contract from `extract_text_from_image`'s text
            # (issue #39 - Batch 5 of the #27/#29/#31 error leak class).
            raise PermanentProcessingError("No text could be extracted from this image") from e
        except Exception as e:
            raise TransientProcessingError("Temporary error extracting text from the image") from e
        return {"text": text}

    async def verify(self, job, file_doc, result):
        if not result.get("text"):
            raise PermanentProcessingError("No text could be extracted from this image")


class ImageCompressProcessor(Processor):
    """job.type == "image_compress" """

    async def validate(self, job, file_doc):
        _validate_image_input(file_doc)

    async def prepare(self, job, file_doc):
        level = (job.params or {}).get("level") if job.params else None
        if level not in _JPEG_QUALITY_BY_LEVEL:
            # Router-side validation (`app/routers/image.py`) already rejects
            # anything outside low/medium/high before a job is even created,
            # so this is a defensive fallback only, not the primary check.
            level = DEFAULT_COMPRESSION_LEVEL
        return {"path": file_doc.storagePath, "level": level}

    async def execute(self, job, file_doc, prepared, ctx=None):
        path = prepared["path"]
        level = prepared["level"]
        is_png = os.path.splitext(file_doc.originalFilename)[1].lower() == ".png"

        try:
            original_size = os.path.getsize(path)
        except OSError as e:
            raise TransientProcessingError("Temporary I/O error while reading the file") from e

        try:
            img = Image.open(path)
            img.load()  # force full decode now - Image.open() alone is lazy
        except (UnidentifiedImageError, OSError) as e:
            # Fixed, hardcoded message rather than `str(e)` - decouples this
            # client-facing contract from Pillow's text (issue #39 - Batch 5
            # of the #27/#29/#31 error leak class).
            raise PermanentProcessingError("File is not a valid image") from e

        os.makedirs(STORAGE_PATH, exist_ok=True)
        output_ext = ".png" if is_png else ".jpg"
        output_path = os.path.join(STORAGE_PATH, f"compress-{job.id}{output_ext}")

        try:
            if is_png:
                # Never force-convert PNG to JPEG (Handbook Part I.2 spec) -
                # re-encode as PNG, relying on `optimize=True` for the size
                # reduction rather than a lossy format change.
                img.save(output_path, "PNG", optimize=True)
            else:
                img.convert("RGB").save(
                    output_path, "JPEG", quality=_JPEG_QUALITY_BY_LEVEL[level], optimize=True
                )
        except OSError as e:
            raise TransientProcessingError("Temporary I/O error while compressing the image") from e
        except Exception as e:
            raise TransientProcessingError("Temporary error while compressing the image") from e

        compressed_size = os.path.getsize(output_path)
        return {
            "output_path": output_path,
            "original_size": original_size,
            "compressed_size": compressed_size,
            # Compressing can legitimately fail to shrink an already-heavily
            # -compressed JPEG or a PNG that just doesn't shrink further -
            # that is NOT a processing failure (Handbook Part I.2 spec), so
            # this is surfaced as a flag on a successful result, not raised.
            "already_optimal": compressed_size >= original_size,
        }

    async def verify(self, job, file_doc, result):
        output_path = result.get("output_path")
        if not output_path or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise PermanentProcessingError("Compression produced no output")
