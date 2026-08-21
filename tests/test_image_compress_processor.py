"""Unit tests for `app.services.image.processors.ImageCompressProcessor`
(Handbook Part C.4, ADR-003), mirroring `tests/test_image_processor.py`'s
pure-unit style for `ImageOcrProcessor`: no Mongo/Redis, a lightweight fake
`file_doc`/`job` (`SimpleNamespace`) is enough since the processor only
reads `file_doc.originalFilename`/`file_doc.storagePath` and
`job.id`/`job.params`.

Real Pillow encode/decode is exercised for real (cheap, unlike
Tesseract/transformers elsewhere in this suite) so the JPEG-quality-vs-size
and PNG-stays-PNG behavior is genuinely verified, not just mocked. The
"already optimal" (compression doesn't shrink the file) branch is tested by
monkeypatching `os.path.getsize` at this module's own import site instead of
trying to contrive a real image that reliably fails to shrink under every
Pillow version/build - that would be flaky by construction; the branch
itself is a plain size comparison (`compressed_size >= original_size`), so
controlling the two `os.path.getsize` return values directly is the
deterministic way to hit it in a unit test.
"""
import os
from types import SimpleNamespace

import pytest
from PIL import Image

from app.services.image.analysis import MAX_IMAGE_PIXELS
from app.services.image.processors import ImageCompressProcessor
from app.services.jobs.processor import PermanentProcessingError, TransientProcessingError

# Same session-scoped loop as every other test module (see
# tests/test_worker_retry.py's comment) - kept for suite-wide consistency
# even though this module never touches Mongo/Redis itself.
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _write_jpeg(path: str, size=(120, 120), quality: int = 95) -> None:
    """A noisy-gradient JPEG (not a flat solid color) so re-encoding at a
    lower quality reliably produces a smaller file - a solid-color image
    compresses to near-nothing at any quality and wouldn't exercise real
    quality-vs-size differences."""
    img = Image.new("RGB", size)
    for x in range(size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), ((x * 37) % 256, (y * 53) % 256, ((x + y) * 17) % 256))
    img.save(path, "JPEG", quality=quality)


def _write_png(path: str, size=(80, 80)) -> None:
    img = Image.new("RGB", size)
    for x in range(size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), (int(255 * x / size[0]), int(255 * y / size[1]), 128))
    img.save(path, "PNG")


def _fake_file_doc(tmp_path, filename: str, writer=None, write_file: bool = True):
    storage_path = str(tmp_path / filename)
    if write_file:
        if writer is not None:
            writer(storage_path)
        else:
            with open(storage_path, "wb") as f:
                f.write(b"not-real-image-bytes-just-a-placeholder")
    return SimpleNamespace(originalFilename=filename, storagePath=storage_path)


def _fake_job(job_id: str, level: str):
    return SimpleNamespace(id=job_id, params={"level": level})


def _cleanup_output(result: dict) -> None:
    output_path = result.get("output_path") if result else None
    if output_path and os.path.exists(output_path):
        os.remove(output_path)


async def test_jpeg_compresses_at_low_quality(tmp_path):
    file_doc = _fake_file_doc(tmp_path, "photo.jpg", writer=lambda p: _write_jpeg(p, quality=95))
    job = _fake_job("low-quality-job", "low")

    result = await ImageCompressProcessor().run(job=job, file_doc=file_doc)
    try:
        assert result["output_path"].endswith(".jpg")
        assert result["compressed_size"] < result["original_size"]
        with Image.open(result["output_path"]) as img:
            assert img.format == "JPEG"
    finally:
        _cleanup_output(result)


async def test_jpeg_compresses_at_medium_quality(tmp_path):
    file_doc = _fake_file_doc(tmp_path, "photo.jpg", writer=lambda p: _write_jpeg(p, quality=95))
    job = _fake_job("medium-quality-job", "medium")

    result = await ImageCompressProcessor().run(job=job, file_doc=file_doc)
    try:
        assert result["compressed_size"] < result["original_size"]
        with Image.open(result["output_path"]) as img:
            assert img.format == "JPEG"
    finally:
        _cleanup_output(result)


async def test_jpeg_compresses_at_high_quality(tmp_path):
    file_doc = _fake_file_doc(tmp_path, "photo.jpg", writer=lambda p: _write_jpeg(p, quality=95))
    job = _fake_job("high-quality-job", "high")

    result = await ImageCompressProcessor().run(job=job, file_doc=file_doc)
    try:
        assert result["compressed_size"] < result["original_size"]
        with Image.open(result["output_path"]) as img:
            assert img.format == "JPEG"
    finally:
        _cleanup_output(result)


async def test_higher_compression_level_produces_smaller_or_equal_output(tmp_path):
    """`_JPEG_QUALITY_BY_LEVEL` maps low=85/medium=65/high=35 - lower Pillow
    `quality` (more compression) for "high" than "medium" than "low", so
    re-encoding the exact same source at each level should never produce a
    *larger* file as the level goes low -> medium -> high."""
    sizes = {}
    for level in ("low", "medium", "high"):
        file_doc = _fake_file_doc(
            tmp_path, f"photo-{level}.jpg", writer=lambda p: _write_jpeg(p, quality=95)
        )
        job = _fake_job(f"ordering-job-{level}", level)
        result = await ImageCompressProcessor().run(job=job, file_doc=file_doc)
        sizes[level] = result["compressed_size"]
        _cleanup_output(result)

    assert sizes["low"] >= sizes["medium"] >= sizes["high"]


async def test_png_input_stays_png_never_force_converted_to_jpeg(tmp_path):
    file_doc = _fake_file_doc(tmp_path, "photo.png", writer=_write_png)
    job = _fake_job("png-stays-png-job", "medium")

    result = await ImageCompressProcessor().run(job=job, file_doc=file_doc)
    try:
        assert result["output_path"].endswith(".png")
        with Image.open(result["output_path"]) as img:
            assert img.format == "PNG"
    finally:
        _cleanup_output(result)


async def test_png_input_saved_with_optimize_true(tmp_path, monkeypatch):
    file_doc = _fake_file_doc(tmp_path, "photo.png", writer=_write_png)
    job = _fake_job("png-optimize-job", "medium")

    captured = {}
    original_save = Image.Image.save

    def _spy_save(self, fp, format=None, **kwargs):
        if format == "PNG":
            captured["format"] = format
            captured["kwargs"] = kwargs
        return original_save(self, fp, format=format, **kwargs)

    monkeypatch.setattr(Image.Image, "save", _spy_save)

    result = await ImageCompressProcessor().run(job=job, file_doc=file_doc)
    try:
        assert captured.get("format") == "PNG"
        assert captured.get("kwargs", {}).get("optimize") is True
    finally:
        _cleanup_output(result)


async def test_already_optimal_when_output_is_not_smaller_completes_without_raising(
    tmp_path, monkeypatch
):
    """The already-highly-compressed-input case (Handbook Part I.2 spec):
    when re-encoding doesn't shrink the file, the job must still complete
    successfully with `already_optimal: True` and both sizes populated -
    this must NOT raise/fail."""
    file_doc = _fake_file_doc(tmp_path, "tiny.jpg", writer=lambda p: _write_jpeg(p, quality=95))
    job = _fake_job("already-optimal-job", "low")

    def _fake_getsize(path):
        if path == file_doc.storagePath:
            return 100
        return 150  # "compressed" output reported bigger than the original

    monkeypatch.setattr("app.services.image.processors.os.path.getsize", _fake_getsize)

    result = await ImageCompressProcessor().run(job=job, file_doc=file_doc)
    try:
        assert result["already_optimal"] is True
        assert result["original_size"] == 100
        assert result["compressed_size"] == 150
    finally:
        monkeypatch.undo()
        _cleanup_output(result)


async def test_not_already_optimal_when_output_is_smaller(tmp_path):
    file_doc = _fake_file_doc(tmp_path, "photo.jpg", writer=lambda p: _write_jpeg(p, quality=95))
    job = _fake_job("not-optimal-job", "high")

    result = await ImageCompressProcessor().run(job=job, file_doc=file_doc)
    try:
        assert result["already_optimal"] is False
        assert result["compressed_size"] < result["original_size"]
    finally:
        _cleanup_output(result)


async def test_corrupt_input_raises_permanent_error_with_fixed_client_safe_message(tmp_path):
    """Fixed, hardcoded message rather than `str(e)` - decouples the
    client-facing contract from Pillow's own exception text (issue #39 -
    Batch 5 of the #27/#29/#31 error-leak class), matching
    `ImageOcrProcessor`'s identical convention."""
    file_doc = _fake_file_doc(tmp_path, "corrupt.jpg", write_file=False)
    with open(file_doc.storagePath, "wb") as f:
        f.write(b"this is not a real jpeg, just garbage bytes\x00\x01\x02")
    job = _fake_job("corrupt-job", "medium")

    with pytest.raises(PermanentProcessingError) as exc_info:
        await ImageCompressProcessor().run(job=job, file_doc=file_doc)

    assert str(exc_info.value) == "File is not a valid image"
    assert exc_info.value.__cause__ is not None


async def test_unsupported_extension_fails_validation_before_execute(tmp_path):
    file_doc = _fake_file_doc(tmp_path, "photo.gif")
    job = _fake_job("unsupported-ext-job", "medium")

    with pytest.raises(PermanentProcessingError, match="supported image"):
        await ImageCompressProcessor().run(job=job, file_doc=file_doc)


async def test_missing_source_file_fails_validation_before_execute(tmp_path):
    file_doc = _fake_file_doc(tmp_path, "gone.jpg", write_file=False)
    assert not os.path.exists(file_doc.storagePath)
    job = _fake_job("missing-source-job", "medium")

    with pytest.raises(PermanentProcessingError, match="missing or has expired"):
        await ImageCompressProcessor().run(job=job, file_doc=file_doc)


async def test_missing_level_falls_back_to_default_medium(tmp_path):
    """Defensive fallback only (the router already rejects anything outside
    low/medium/high before a job is created - see `app/routers/image.py`),
    but `prepare()` must not blow up if `job.params` is missing/invalid -
    it should fall back to `DEFAULT_COMPRESSION_LEVEL` ("medium")."""
    file_doc = _fake_file_doc(tmp_path, "photo.jpg", writer=lambda p: _write_jpeg(p, quality=95))
    job = SimpleNamespace(id="no-level-job", params=None)

    result = await ImageCompressProcessor().run(job=job, file_doc=file_doc)
    try:
        assert result["compressed_size"] < result["original_size"]
    finally:
        _cleanup_output(result)


async def test_execute_wraps_getsize_oserror_as_transient(tmp_path, monkeypatch):
    file_doc = _fake_file_doc(tmp_path, "photo.jpg", writer=lambda p: _write_jpeg(p, quality=95))
    job = _fake_job("transient-getsize-job", "medium")

    def _boom(path):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.image.processors.os.path.getsize", _boom)

    with pytest.raises(TransientProcessingError):
        await ImageCompressProcessor().run(job=job, file_doc=file_doc)


def _write_oversized_png(path: str) -> None:
    """Just over `MAX_IMAGE_PIXELS` - a solid color so it still encodes
    quickly despite the huge declared dimensions, mirroring the real-world
    decompression-bomb shape from the security review finding (2026-08-21):
    a small file, an enormous pixel count."""
    width = 7000
    height = (MAX_IMAGE_PIXELS // width) + 1000  # comfortably over the cap
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(path, "PNG")


async def test_oversized_image_rejected_before_full_decode(tmp_path):
    """Guards the fix for the confirmed decompression-bomb finding: a huge
    declared-pixel-count image must be rejected right after `Image.open()`
    (cheap - header only), before `.load()` decodes the full pixel buffer."""
    file_doc = _fake_file_doc(tmp_path, "huge.png", writer=_write_oversized_png)
    job = _fake_job("oversized-job", "medium")

    with pytest.raises(PermanentProcessingError, match="exceed the maximum allowed size"):
        await ImageCompressProcessor().run(job=job, file_doc=file_doc)


async def test_verify_rejects_missing_output_file_directly():
    """Defense-in-depth: even if `execute()` somehow returned a path that
    doesn't exist on disk, `verify()` on its own must still reject it -
    mirrors `ImageOcrProcessor.verify`'s equivalent direct-call test."""
    with pytest.raises(PermanentProcessingError, match="no output"):
        await ImageCompressProcessor().verify(
            job=object(), file_doc=object(), result={"output_path": "/nonexistent/path.jpg"}
        )
