import logging
from datetime import datetime

logger = logging.getLogger(__name__)

def calculate_age(birth_date: str) -> int:
    """Calculate age from birth date in YYYY-MM-DD format."""
    try:
        # Validate date format
        try:
            birth = datetime.strptime(birth_date, "%Y-%m-%d")
        except ValueError as e:
            logger.error("❌ Invalid date format: %s", str(e))
            raise ValueError("Invalid date format. Use YYYY-MM-DD")
        
        today = datetime.utcnow()
        logger.debug("📅 Today: %s, Birth: %s", today, birth)
        
        # Check if birth date is in the future
        if birth > today:
            logger.error("❌ Birth date is in the future: %s", birth_date)
            raise ValueError("Birth date cannot be in the future")
        
        # Calculate age
        age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
        logger.debug("✅ Age calculated: %s years", age)
        return age
    except Exception as e:
        logger.exception("💥 Error calculating age: %s", str(e))
        raise