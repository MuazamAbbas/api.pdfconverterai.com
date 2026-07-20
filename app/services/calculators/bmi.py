import logging

logger = logging.getLogger(__name__)

def calculate_bmi(weight_kg: float, height_m: float) -> float:
    """Calculate BMI from weight (kg) and height (m)."""
    try:
        if weight_kg <= 0 or height_m <= 0:
            logger.error("❌ Invalid input: weight=%s, height=%s", weight_kg, height_m)
            raise ValueError("Weight and height must be positive")
        bmi = weight_kg / (height_m ** 2)
        logger.debug("✅ BMI calculated: %s", bmi)
        return round(bmi, 2)
    except Exception as e:
        logger.exception("💥 Error calculating BMI: %s", str(e))
        raise