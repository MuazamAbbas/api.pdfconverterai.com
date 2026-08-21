"""Unit tests for `app.services.image.analysis` (Handbook Part I.2 / C.9):
the Tier 1, instant, no-Job logic behind `POST /image/file_info` and
`POST /image/color_palette` (`app/routers/image.py`).

Pure unit tests (no Mongo/Redis, no HTTP client) - mirrors
`tests/test_image_processor.py`'s pure-unit style. These call
`get_image_info`/`get_color_palette` directly against real Pillow-encoded
in-memory bytes (real PNG/JPEG round trips, not mocked - Pillow itself is
cheap enough to exercise for real here, unlike Tesseract/transformers
elsewhere in this suite), plus a corrupt-content case to verify the
issue #39 / Batch 5 client-safe-error convention (`ValueError` with a fixed
message, no raw Pillow exception text) that
`app/routers/image.py::image_file_info`/`image_color_palette` rely on to
build their `FILE_INVALID_CONTENT` response.
"""
import io

import pytest
from PIL import Image

from app.services.image.analysis import get_color_palette, get_image_info

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _png_bytes(size=(40, 30), color=(255, 0, 0)) -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(size=(50, 20), color=(0, 128, 255)) -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _multi_color_png_bytes(size=(60, 60)) -> bytes:
    """A PNG with a smooth RGB gradient - hundreds of genuinely distinct
    pixel colors (unlike a handful of flat solid blocks), so
    MEDIANCUT-quantizing down to `DEFAULT_PALETTE_COLORS` (6) actually
    produces 6 distinct clusters instead of collapsing to however many flat
    blocks happened to be drawn. A real uploaded photo (this tool's actual
    use case) has this same kind of pixel variety."""
    img = Image.new("RGB", size)
    for x in range(size[0]):
        for y in range(size[1]):
            img.putpixel(
                (x, y),
                (
                    int(255 * x / size[0]),
                    int(255 * y / size[1]),
                    int(255 * ((x + y) % size[0]) / size[0]),
                ),
            )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def test_get_image_info_returns_expected_shape_for_png():
    content = _png_bytes(size=(40, 30))
    info = await get_image_info(content)

    assert info["width"] == 40
    assert info["height"] == 30
    assert info["format"] == "PNG"
    assert info["mode"] == "RGB"
    assert info["size_bytes"] == len(content)
    assert info["size_kb"] == round(len(content) / 1024, 2)
    assert info["size_mb"] == round(len(content) / (1024 * 1024), 2)


async def test_get_image_info_returns_expected_shape_for_jpeg():
    content = _jpeg_bytes(size=(50, 20))
    info = await get_image_info(content)

    assert info["width"] == 50
    assert info["height"] == 20
    assert info["format"] == "JPEG"
    assert info["mode"] == "RGB"
    assert info["size_bytes"] == len(content)


async def test_get_image_info_rejects_corrupt_image():
    """`ValueError` with a generic message - never `str(e)` from Pillow's
    own `UnidentifiedImageError`/`OSError` text (the router turns this into
    a fixed 400 `FILE_INVALID_CONTENT`, not a passthrough of whatever
    Pillow says - issue #39 / Batch 5 convention)."""
    corrupt = b"this is not a real image, just garbage bytes\x00\x01\x02"

    with pytest.raises(ValueError):
        await get_image_info(corrupt)


async def test_get_color_palette_returns_six_entries_sorted_descending_summing_sanely():
    content = _multi_color_png_bytes()
    colors = await get_color_palette(content)

    assert len(colors) == 6
    for entry in colors:
        assert set(entry.keys()) == {"hex", "rgb", "percentage"}
        assert entry["hex"].startswith("#") and len(entry["hex"]) == 7
        assert len(entry["rgb"]) == 3
        assert all(0 <= c <= 255 for c in entry["rgb"])
        assert 0 <= entry["percentage"] <= 100

    percentages = [c["percentage"] for c in colors]
    assert percentages == sorted(percentages, reverse=True), "must be sorted descending"

    total = sum(percentages)
    # Rounded per-entry percentages of a handful of solid blocks should sum
    # close to 100 (allowing for per-entry rounding drift), not some wildly
    # off value.
    assert 95 <= total <= 100.5


async def test_get_color_palette_respects_num_colors_param():
    content = _multi_color_png_bytes()
    colors = await get_color_palette(content, num_colors=3)
    assert len(colors) <= 3


async def test_get_color_palette_rejects_corrupt_image():
    corrupt = b"this is not a real image, just garbage bytes\x00\x01\x02"

    with pytest.raises(ValueError):
        await get_color_palette(corrupt)
