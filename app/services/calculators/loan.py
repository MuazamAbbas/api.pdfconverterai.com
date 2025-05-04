import logging
import math

logger = logging.getLogger(__name__)

def calculate_loan(principal: float, annual_rate: float, years: int) -> dict:
    """Calculate monthly loan payment and total interest."""
    try:
        if principal <= 0 or annual_rate < 0 or years <= 0:
            logger.error("❌ Invalid inputs: principal=%s, annual_rate=%s, years=%s", principal, annual_rate, years)
            raise ValueError("Principal and years must be positive, rate non-negative")
        
        monthly_rate = annual_rate / 100 / 12
        months = years * 12
        if monthly_rate == 0:
            monthly_payment = principal / months
            total_interest = 0
        else:
            monthly_payment = principal * (monthly_rate * (1 + monthly_rate)**months) / ((1 + monthly_rate)**months - 1)
            total_interest = (monthly_payment * months) - principal
        
        logger.debug("✅ Loan calculated: monthly_payment=%s, total_interest=%s", monthly_payment, total_interest)
        return {
            "monthly_payment": round(monthly_payment, 2),
            "total_interest": round(total_interest, 2)
        }
    except Exception as e:
        logger.exception("💥 Error calculating loan: %s", str(e))
        raise