import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import verify_api_key
from app.models.unit_converters import (
    AngleConvertRequest,
    AreaConvertRequest,
    ContextualConvertRequest,
    DataConvertRequest,
    DensityConvertRequest,
    EnergyConvertRequest,
    FlowRateConvertRequest,
    ForceConvertRequest,
    FrequencyConvertRequest,
    FuelEfficiencyConvertRequest,
    LengthConvertRequest,
    PowerConvertRequest,
    PressureConvertRequest,
    SpeedConvertRequest,
    TemperatureConvertRequest,
    TimeConvertRequest,
    TorqueConvertRequest,
    VolumeConvertRequest,
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

@router.post("/area", summary="Convert area between units")
async def convert_area(request: AreaConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting area: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "square_meter": 1.0,  # Base unit
        "square_kilometer": 1_000_000.0,  # 1 square kilometer = 1,000,000 square meters
        "square_foot": 0.09290304,  # 1 square foot = 0.09290304 square meters
        "square_yard": 0.83612736,  # 1 square yard = 0.83612736 square meters
        "acre": 4046.8564224  # 1 acre = 4046.8564224 square meters
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
        logger.exception("💥 Error converting area: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting area: {str(e)}")

@router.post("/volume", summary="Convert volume between units")
async def convert_volume(request: VolumeConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting volume: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "liter": 1.0,  # Base unit
        "milliliter": 0.001,  # 1 liter = 1000 milliliters
        "cubic_meter": 1000.0,  # 1 cubic meter = 1000 liters
        "gallon": 3.785411784,  # 1 gallon (US) = 3.785411784 liters
        "cubic_foot": 28.316846592  # 1 cubic foot = 28.316846592 liters
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
        logger.exception("💥 Error converting volume: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting volume: {str(e)}")

@router.post("/speed", summary="Convert speed between units")
async def convert_speed(request: SpeedConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting speed: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "meter_per_second": 1.0,  # Base unit
        "kilometer_per_hour": 0.277777778,  # 1 km/h = 0.277777778 m/s
        "mile_per_hour": 0.44704,  # 1 mph = 0.44704 m/s
        "knot": 0.514444444  # 1 knot = 0.514444444 m/s
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
        logger.exception("💥 Error converting speed: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting speed: {str(e)}")

@router.post("/time", summary="Convert time between units")
async def convert_time(request: TimeConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting time: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "second": 1.0,  # Base unit
        "minute": 60.0,  # 1 minute = 60 seconds
        "hour": 3600.0,  # 1 hour = 3600 seconds
        "day": 86400.0,  # 1 day = 86400 seconds
        "week": 604800.0  # 1 week = 604800 seconds
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
        logger.exception("💥 Error converting time: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting time: {str(e)}")

@router.post("/data", summary="Convert data storage size between units")
async def convert_data(request: DataConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting data: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "byte": 1.0,  # Base unit
        "kilobyte": 1024.0,  # 1 KB = 1024 bytes (binary)
        "megabyte": 1024.0 ** 2,  # 1 MB = 1024^2 bytes (binary)
        "gigabyte": 1024.0 ** 3,  # 1 GB = 1024^3 bytes (binary)
        "terabyte": 1024.0 ** 4  # 1 TB = 1024^4 bytes (binary)
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
        logger.exception("💥 Error converting data: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting data: {str(e)}")

@router.post("/energy", summary="Convert energy between units")
async def convert_energy(request: EnergyConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting energy: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "joule": 1.0,  # Base unit
        "kilojoule": 1000.0,  # 1 kilojoule = 1000 joules
        "calorie": 4.184,  # 1 calorie = 4.184 joules
        "kilocalorie": 4184.0,  # 1 kilocalorie = 4184 joules
        "watt_hour": 3600.0  # 1 watt-hour = 3600 joules
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
        logger.exception("💥 Error converting energy: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting energy: {str(e)}")

@router.post("/power", summary="Convert power between units")
async def convert_power(request: PowerConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting power: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "watt": 1.0,  # Base unit
        "kilowatt": 1000.0,  # 1 kilowatt = 1000 watts
        "horsepower": 745.699872,  # 1 horsepower = 745.699872 watts
        "megawatt": 1_000_000.0  # 1 megawatt = 1,000,000 watts
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
        logger.exception("💥 Error converting power: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting power: {str(e)}")

@router.post("/pressure", summary="Convert pressure between units")
async def convert_pressure(request: PressureConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting pressure: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "pascal": 1.0,  # Base unit
        "atmosphere": 101325.0,  # 1 atmosphere = 101325 pascals
        "bar": 100000.0,  # 1 bar = 100000 pascals
        "psi": 6894.757293168  # 1 psi = 6894.757293168 pascals
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
        logger.exception("💥 Error converting pressure: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting pressure: {str(e)}")

@router.post("/frequency", summary="Convert frequency between units")
async def convert_frequency(request: FrequencyConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting frequency: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "hertz": 1.0,  # Base unit
        "kilohertz": 1_000.0,  # 1 kilohertz = 1000 hertz
        "megahertz": 1_000_000.0,  # 1 megahertz = 1,000,000 hertz
        "gigahertz": 1_000_000_000.0  # 1 gigahertz = 1,000,000,000 hertz
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
        logger.exception("💥 Error converting frequency: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting frequency: {str(e)}")

@router.post("/force", summary="Convert force between units")
async def convert_force(request: ForceConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting force: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "newton": 1.0,  # Base unit
        "kilonewton": 1000.0,  # 1 kilonewton = 1000 newtons
        "pound_force": 4.4482216152605,  # 1 pound-force = 4.4482216152605 newtons
        "kilogram_force": 9.80665  # 1 kilogram-force = 9.80665 newtons
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
        logger.exception("💥 Error converting force: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting force: {str(e)}")

@router.post("/torque", summary="Convert torque between units")
async def convert_torque(request: TorqueConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting torque: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "newton_meter": 1.0,  # Base unit
        "foot_pound": 1.3558179483314,  # 1 foot-pound = 1.3558179483314 newton-meters
        "inch_pound": 0.1129848290276,  # 1 inch-pound = 0.1129848290276 newton-meters
        "kilogram_force_meter": 9.80665  # 1 kilogram-force meter = 9.80665 newton-meters
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
        logger.exception("💥 Error converting torque: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting torque: {str(e)}")

@router.post("/density", summary="Convert density between units")
async def convert_density(request: DensityConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting density: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "kg_per_cubic_meter": 1.0,  # Base unit
        "g_per_cubic_cm": 1000.0,  # 1 g/cm3 = 1000 kg/m3
        "lb_per_cubic_ft": 16.018463374,  # 1 lb/ft3 = 16.018463374 kg/m3
        "kg_per_liter": 1000.0  # 1 kg/L = 1000 kg/m3
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
        logger.exception("💥 Error converting density: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting density: {str(e)}")

@router.post("/flow_rate", summary="Convert flow rate between units")
async def convert_flow_rate(request: FlowRateConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting flow rate: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "liter_per_second": 1.0,  # Base unit
        "cubic_meter_per_hour": 0.277777778,  # 1 m3/h = 0.277777778 L/s
        "gallon_per_minute": 0.0630901964,  # 1 gal/min = 0.0630901964 L/s
        "cubic_foot_per_minute": 0.4719474432  # 1 ft3/min = 0.4719474432 L/s
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
        logger.exception("💥 Error converting flow rate: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting flow rate: {str(e)}")

@router.post("/angle", summary="Convert angle between units")
async def convert_angle(request: AngleConvertRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Converting angle: %s %s to %s", request.value, request.from_unit, request.to_unit)
    units = {
        "degree": 0.017453292519943,  # 1 degree = 0.017453292519943 radians
        "radian": 1.0,  # Base unit
        "gradian": 0.015707963267949,  # 1 gradian = 0.015707963267949 radians
        "turn": 6.283185307179586  # 1 turn = 6.283185307179586 radians
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
        logger.exception("💥 Error converting angle: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting angle: {str(e)}")

@router.post("/fuel_efficiency", summary="Convert fuel efficiency between units")
async def convert_fuel_efficiency(
    request: FuelEfficiencyConvertRequest, api_key: dict = Depends(verify_api_key)
):
    logger.debug(
        "🔧 Converting fuel efficiency: %s %s to %s",
        request.value, request.from_unit, request.to_unit
    )
    valid_units = {"mpg", "km_per_liter", "l_per_100km"}
    if request.from_unit not in valid_units or request.to_unit not in valid_units:
        logger.error("❌ Invalid unit: %s or %s", request.from_unit, request.to_unit)
        raise HTTPException(status_code=400, detail="Invalid unit")
    if request.from_unit == request.to_unit:
        logger.debug(
            "✅ Conversion result: %s %s = %s %s",
            request.value, request.from_unit, request.value, request.to_unit
        )
        return {
            "value": request.value,
            "from_unit": request.from_unit,
            "to_unit": request.to_unit,
            "result": round(request.value, 4)
        }
    try:
        # Fuel efficiency conversions are reciprocal (L/100km is inversely
        # proportional to km/L), not a simple multiplier-through-origin like
        # length's `units` dict - convert from_unit to km_per_liter first as
        # an internal base, then km_per_liter to to_unit, mirroring
        # convert_temperature's affine from->base->to pattern above, but
        # with division instead of an offset.
        MPG_TO_KPL = 0.425143707  # 1 mile per US gallon = 0.425143707 km/L

        if request.from_unit == "km_per_liter":
            km_per_liter = request.value
        elif request.from_unit == "mpg":
            km_per_liter = request.value * MPG_TO_KPL
        else:  # l_per_100km - reciprocal of km/L, not a multiplier
            if request.value == 0:
                raise ZeroDivisionError
            km_per_liter = 100 / request.value

        if request.to_unit == "km_per_liter":
            result = km_per_liter
        elif request.to_unit == "mpg":
            result = km_per_liter / MPG_TO_KPL
        else:  # l_per_100km
            if km_per_liter == 0:
                raise ZeroDivisionError
            result = 100 / km_per_liter

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
    except ZeroDivisionError:
        logger.error(
            "❌ Invalid value for fuel efficiency conversion: %s %s to %s (division by zero)",
            request.value, request.from_unit, request.to_unit
        )
        raise HTTPException(
            status_code=400, detail="Invalid value: cannot convert a value of zero for this unit"
        )
    except Exception as e:
        logger.exception("💥 Error converting fuel efficiency: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error converting fuel efficiency: {str(e)}")

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