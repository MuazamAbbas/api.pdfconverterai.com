import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import verify_api_key
from app.shared.responses import api_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/binary_tools", tags=["binary_tools"])

class TextToBinaryRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000)

class BinaryToTextRequest(BaseModel):
    # Generous but bounded: ~100000 chars is well beyond any legitimate
    # use (tens of thousands of words), but still caps the synchronous
    # per-group validation loop below to a bounded, cheap-to-reject size
    # (Handbook Part C - Tier 1 endpoints must not do unbounded sync work
    # inside `async def` handlers on a 2-worker gunicorn config).
    binary: str = Field(..., min_length=1, max_length=100000)

@router.get("/test", summary="Test Binary Tools endpoint")
async def test_binary_tools():
    logger.debug("🧪 Testing Binary Tools endpoint")
    return {"message": "Binary Tools router is working"}

@router.post("/text_to_binary", summary="Convert text to binary")
async def text_to_binary(request: TextToBinaryRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting text to binary: %s", request.text)
    try:
        # Encode via UTF-8 bytes (not codepoints via ord()) so every group is
        # exactly 8 bits regardless of script/emoji - ord() overflows the
        # '08b' format for any codepoint above 255.
        binary = ' '.join(format(byte, '08b') for byte in request.text.encode('utf-8'))
        logger.debug("✅ Text converted to binary: %s", binary)
        return {"text": request.text, "binary": binary}
    except Exception as e:
        logger.exception("💥 Error converting text to binary: %s", str(e))
        raise api_error(500, "Failed to convert text to binary", "BINARY_CONVERSION_FAILED")

@router.post("/binary_to_text", summary="Convert binary to text")
async def binary_to_text(request: BinaryToTextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting binary to text: %s", request.binary)

    groups = request.binary.split()
    for group in groups:
        if len(group) != 8 or any(bit not in "01" for bit in group):
            logger.error("❌ Invalid binary group in input")
            raise api_error(
                400,
                "Invalid binary format: each group must be exactly 8 bits of 0s and 1s.",
                "INVALID_BINARY_FORMAT",
            )

    try:
        byte_values = bytes(int(group, 2) for group in groups)
        decoded_text = byte_values.decode('utf-8')
    except UnicodeDecodeError:
        logger.error("❌ Decoded binary is not valid UTF-8")
        raise api_error(
            400,
            "The binary data does not represent valid UTF-8 text.",
            "INVALID_UTF8_SEQUENCE",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("💥 Error converting binary to text: %s", str(e))
        raise api_error(500, "Failed to convert binary to text", "BINARY_TO_TEXT_CONVERSION_FAILED")

    logger.debug("✅ Binary converted to text: %s", decoded_text)
    return {"binary": request.binary, "text": decoded_text}