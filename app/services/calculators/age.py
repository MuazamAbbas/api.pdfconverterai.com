import calendar
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

def calculate_age(birth_date: str) -> dict:
    """Calculate full age breakdown (years, months, days) from birth date in YYYY-MM-DD format."""
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

        # Calculate full years/months/days breakdown
        years = today.year - birth.year
        months = today.month - birth.month
        days = today.day - birth.day

        if days < 0:
            months -= 1
            # Borrow days from the month preceding "today"
            prev_month = today.month - 1 or 12
            prev_month_year = today.year if today.month > 1 else today.year - 1
            days_in_prev_month = calendar.monthrange(prev_month_year, prev_month)[1]
            days += days_in_prev_month

        if months < 0:
            years -= 1
            months += 12

        age = {"years": years, "months": months, "days": days}
        logger.debug("✅ Age calculated: %s", age)
        return age
    except Exception as e:
        logger.exception("💥 Error calculating age: %s", str(e))
        raise