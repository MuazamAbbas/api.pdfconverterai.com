import io
import logging
from datetime import datetime
from typing import Literal, Optional

import qrcode
import qrcode.constants
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.core.security import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/miscellaneous", tags=["miscellaneous"])

# Maps the validated `error_correction` request field to the qrcode library's
# constants (Handbook Part I.2 - Tier 1, no job queue, plain sync endpoint).
_QR_ERROR_CORRECTION_LEVELS = {
    "L": qrcode.constants.ERROR_CORRECT_L,
    "M": qrcode.constants.ERROR_CORRECT_M,
    "Q": qrcode.constants.ERROR_CORRECT_Q,
    "H": qrcode.constants.ERROR_CORRECT_H,
}


class QRCodeRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    size: Optional[int] = Field(default=300, ge=100, le=1000)
    error_correction: Optional[Literal["L", "M", "Q", "H"]] = Field(default="M")


@router.get("/test", summary="Test Miscellaneous endpoint")
async def test_miscellaneous():
    logger.debug("🧪 Testing Miscellaneous endpoint")
    return {"message": "Miscellaneous router is working"}

@router.get("/timestamp", summary="Get current timestamp")
async def get_timestamp(api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Generating timestamp")
    try:
        timestamp = int(datetime.utcnow().timestamp())
        logger.debug("✅ Timestamp generated: %d", timestamp)
        return {"timestamp": timestamp}
    except Exception as e:
        logger.exception("💥 Error generating timestamp: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error generating timestamp: {str(e)}")

@router.post("/qr_code", summary="Generate a QR code PNG image from text")
async def generate_qr_code(payload: QRCodeRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug(
        "🔧 Generating QR code (size=%s, error_correction=%s)",
        payload.size,
        payload.error_correction,
    )
    try:
        # box_size (pixels-per-module) can't be computed up front: module
        # count depends on both text length and error_correction and ranges
        # from 21 to 177, not a fixed ~30. So build the QR without a final
        # box_size first, let `qr.make(fit=True)` pick the actual version/
        # module count, then size the boxes from that.
        qr = qrcode.QRCode(
            error_correction=_QR_ERROR_CORRECTION_LEVELS[payload.error_correction],
            border=4,
        )
        qr.add_data(payload.text)

        try:
            qr.make(fit=True)
        except ValueError:
            # The qrcode library raises a bare ValueError("Invalid version
            # (was 41, expected 1 to 40)") from its internal version-fitting
            # step when the text doesn't fit in any QR version (1-40) at the
            # requested error_correction level. Within this scope (inputs
            # are already Pydantic-validated: text is a plain str of 1-2000
            # chars, error_correction is one of L/M/Q/H, box_size/border are
            # fixed by us) this is the only condition that raises ValueError
            # here, so it maps cleanly to a client-facing 400.
            logger.warning(
                "⚠️ QR code capacity exceeded (text_len=%d, error_correction=%s)",
                len(payload.text),
                payload.error_correction,
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    "Text is too long to encode at the selected error "
                    "correction level. Try shorter text or a lower error "
                    "correction level (L or M)."
                ),
            )

        # qr.modules_count is set by make() to the *actual* module count for
        # the version it picked (module count = version * 4 + 17, ranging
        # 21-177). Size box_size off that plus the quiet-zone border on both
        # sides so the final PNG lands close to the requested `size`. The
        # max(1, ...) guard is still meaningful here: modules_count + border
        # can exceed `size` for a large version (e.g. version 40 -> 177 + 8
        # = 185 modules, which is > the smallest allowed size of 100),
        # which would otherwise floor-divide to 0.
        total_modules = qr.modules_count + 2 * qr.border
        box_size = max(1, payload.size // total_modules)
        qr.box_size = box_size

        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        png_bytes = buffer.getvalue()

        logger.debug("✅ QR code generated: %d bytes", len(png_bytes))
        return Response(content=png_bytes, media_type="image/png")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("💥 Error generating QR code: %s", str(e))
        raise HTTPException(status_code=500, detail="Error generating QR code")
