"""Tier 1 (instant, no Job - Handbook Part I.2) image analysis logic for the
`image` module: File Size Checker (`POST /image/file_info`) and Color
Palette Generator (`POST /image/color_palette`) in `app/routers/image.py`.

Both take already-validated, in-memory bytes (the router validates
extension/size/magic-bytes via `app/services/files/service.py`'s
`read_and_validate_upload` before calling into here - neither of these
tools persists a `files` record or writes to disk, Handbook Part C.9) and
raise a plain `ValueError` on a corrupt/unparseable image, mirroring
`app/services/image/ocr.py`'s `extract_text_from_image` convention so the
router can catch one exception type and turn it into a fixed, client-safe
error message rather than leaking raw Pillow exception text (issue #39 -
Batch 5 of the #27/#29/#31 error leak class).
"""
import io
import logging

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

DEFAULT_PALETTE_COLORS = 6

# ~40 megapixels - comfortably covers real photos (a 25MB upload cap makes
# anything beyond this implausible as a legitimate image) while blocking a
# "pixel flood" decompression bomb: a tiny, valid PNG/JPEG can declare huge
# dimensions that decode to a multi-hundred-MB in-memory buffer and pin a
# CPU core for seconds once something actually touches pixel data
# (`.load()`/`.quantize()`/`.save()`). `Image.open()` itself only reads the
# header and is cheap - checking `img.width * img.height` right after it,
# before `.load()`, is what actually prevents the decode (confirmed exploit
# via security-reviewer, 2026-08-21). Pillow's own `MAX_IMAGE_PIXELS`
# default is a *warning* until 2x that value, not a hard stop, so this
# module enforces its own cap rather than relying on that.
MAX_IMAGE_PIXELS = 40_000_000


def _open_image(image_data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(image_data))
        if img.width * img.height > MAX_IMAGE_PIXELS:
            raise ValueError("Image dimensions exceed the maximum allowed size")
        img.load()  # force full decode now - Image.open() alone is lazy
        return img
    except (UnidentifiedImageError, OSError) as e:
        logger.warning("Could not parse uploaded image: %s", str(e))
        raise ValueError("File is not a valid image") from e


async def get_image_info(image_data: bytes) -> dict:
    """File Size Checker: size (bytes/KB/MB), dimensions, format, color mode."""
    img = _open_image(image_data)
    size_bytes = len(image_data)
    return {
        "size_bytes": size_bytes,
        "size_kb": round(size_bytes / 1024, 2),
        "size_mb": round(size_bytes / (1024 * 1024), 2),
        "width": img.width,
        "height": img.height,
        "format": img.format,
        "mode": img.mode,
    }


async def get_color_palette(image_data: bytes, num_colors: int = DEFAULT_PALETTE_COLORS) -> list[dict]:
    """Color Palette Generator: top `num_colors` dominant colors, sorted by
    descending share of pixels, as `{hex, rgb: [r, g, b], percentage}`.
    """
    img = _open_image(image_data)
    quantized = (
        img.convert("RGB")
        .quantize(colors=num_colors, method=Image.Quantize.MEDIANCUT)
        .convert("RGB")
    )
    color_counts = quantized.getcolors(maxcolors=256)
    if not color_counts:
        raise ValueError("Could not extract colors from this image")

    total = sum(count for count, _ in color_counts) or 1
    palette = [
        {
            "hex": "#{:02x}{:02x}{:02x}".format(r, g, b),
            "rgb": [r, g, b],
            "percentage": round((count / total) * 100, 2),
        }
        for count, (r, g, b) in sorted(color_counts, key=lambda c: c[0], reverse=True)
    ]
    return palette
