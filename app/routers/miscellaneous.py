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
        # box_size is pixels-per-module; the final PNG dimensions also
        # depend on the QR version (module count) that `qr.make(fit=True)`
        # picks based on data length, so this is a reasonable approximation
        # of the requested `size`, not an exact pixel guarantee.
        box_size = max(1, payload.size // 30)
        qr = qrcode.QRCode(
            error_correction=_QR_ERROR_CORRECTION_LEVELS[payload.error_correction],
            box_size=box_size,
            border=4,
        )
        qr.add_data(payload.text)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        png_bytes = buffer.getvalue()

        logger.debug("✅ QR code generated: %d bytes", len(png_bytes))
        return Response(content=png_bytes, media_type="image/png")
    except Exception as e:
        logger.exception("💥 Error generating QR code: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error generating QR code: {str(e)}")
