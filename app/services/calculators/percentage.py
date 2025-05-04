import logging

logger = logging.getLogger(__name__)

def calculate_percentage(value: float, percentage: float) -> float:
    """Calculate percentage of a value."""
    try:
        if value < 0 or percentage < 0:
            logger.error("❌ Negative values not allowed: value=%s, percentage=%s", value, percentage)
            raise ValueError("Negative values not allowed")
        result = (percentage / 100) * value
        logger.debug("✅ Percentage calculated: %s%% of %s = %s", percentage, value, result)
        return result
    except Exception as e:
        logger.exception("💥 Error calculating percentage: %s", str(e))
        raise