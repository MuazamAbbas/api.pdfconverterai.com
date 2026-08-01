import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import verify_api_key
from app.models.unit_converters import (
    ContextualConvertRequest,
    LengthConvertRequest,
    TemperatureConvertRequest,
    WeightConvertRequest,
)
from app.services.unit_converters.contextual_convert import contextual_convert

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
        "kilometer": 1000.0,  # 1 meter = 1000 kilometers
        "centimeter": 0.01,  # 1 meter = 100 centimeters
        "mile": 1609.344,  # 1 mile = 1609.344 meters
        "yard": 0.9144  # 1 yard = 0.9144 meters
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

@router.post("/temperature", summary="Convert temperature between units")
async def convert_temperature(
    request: TemperatureConvertRequest, api_key: dict = Depends(verify_api_key)
):
    logger.debug(
        "🔧 Converting temperature: %s %s to %s",
        request.value, request.from_unit, request.to_unit
    )
    valid_units = {"celsius", "fahrenheit", "kelvin"}
    if request.from_unit not in valid_units or request.to_unit not in valid_units:
        logger.error("❌ Invalid unit: %s or %s", request.from_unit, request.to_unit)
        raise HTTPException(status_code=400, detail="Invalid unit")
    try:
        # Temperature conversions are affine (offset-based), not simple
        # multiplier-through-origin like length's `units` dict - convert
        # from_unit to Celsius first as an internal base, then Celsius to
        # to_unit, instead of a single ratio.
        if request.from_unit == "celsius":
            celsius = request.value
        elif request.from_unit == "fahrenheit":
            celsius = (request.value - 32) * 5 / 9
        else:  # kelvin
            celsius = request.value - 273.15

        if request.to_unit == "celsius":
            result = celsius
        elif request.to_unit == "fahrenheit":
            result = celsius * 9 / 5 + 32
        else:  # kelvin
            result = celsius + 273.15

        logger.debug(
            "✅ Conversion result: %s %s = %s %s",
            request.value, request.from_unit, result, request.to_unit
        )
        return {
            "value": request.value,
            "from_unit": request.from_unit,
            "to_unit": request.to_unit,
            "result": round(result, 4)
        }
    except Exception as e:
        logger.exception("💥 Error converting temperature: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting temperature: {str(e)}")

@router.post("/weight", summary="Convert weight between units")
async def convert_weight(request: WeightConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting weight: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "kilogram": 1.0,  # Base unit
        "gram": 0.001,  # 1 kilogram = 1000 grams
        "pound": 0.45359237,  # 1 pound = 0.45359237 kilograms
        "ounce": 0.028349523125  # 1 ounce = 0.028349523125 kilograms
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
        logger.exception("💥 Error converting weight: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting weight: {str(e)}")

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