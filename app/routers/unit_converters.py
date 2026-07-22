from fastapi import APIRouter, HTTPException, Depends
import logging
from app.core.security import verify_api_key
from app.core.config import settings
from app.models.unit_converters import LengthConvertRequest, ContextualConvertRequest
from app.services.unit_converters.contextual_convert import contextual_convert

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/unit_converters", tags=["Unit Converters"])

@router.get("/test", summary="Test Unit Converters endpoint")
async def test_unit_converters(api_key: dict = Depends(verify_api_key)):
    logger.debug("🧪 Testing Unit Converters endpoint")
    return {"message": "Unit Converters router is working"}

@router.post("/length", summary="Convert length between units")
async def convert_length(request: LengthConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting length: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "meter": 1.0,  # Base unit
        "foot": 0.3048,  # 1 meter = 0.3048 feet
        "inch": 0.0254,  # 1 meter = 0.0254 inches
        "kilometer": 1000.0  # 1 meter = 1000 kilometers
    }
    if request.from_unit not in units or request.to_unit not in units:
        logger.error("❌ Invalid unit: %s or %s", request.from_unit, request.to_unit)
        raise HTTPException(status_code=400, detail="Invalid unit")
    try:
        result = request.value * units[request.from_unit] / units[request.to_unit]
        logger.debug("✅ Conversion result: %s %s = %s %s", request.value, request.from_unit, result, request.to_unit)
        return {
            "value": request.value,
            "from_unit": request.from_unit,
            "to_unit": request.to_unit,
            "result": round(result, 4)
        }
    except Exception as e:
        logger.exception("💥 Error converting length: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting length: {str(e)}")

@router.post("/convert", summary="Convert units using natural language query")
async def contextual_convert_endpoint(request: ContextualConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting units for query: %s", request.query)
    try:
        result = await contextual_convert(request.query)
        logger.debug("✅ Conversion result: %s", result)
        return result
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error converting units: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting units: {str(e)}")