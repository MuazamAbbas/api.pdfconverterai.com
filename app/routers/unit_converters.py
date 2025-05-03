from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import logging
from app.core.security import verify_api_key

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/unit_converters", tags=["unit_converters"])

class LengthConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

@router.get("/test", summary="Test Unit Converters endpoint")
async def test_unit_converters():
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