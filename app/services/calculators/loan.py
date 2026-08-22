import logging

logger = logging.getLogger(__name__)

def calculate_loan(principal: float, annual_rate: float, years: int) -> dict:
    """Calculate monthly loan payment and total interest."""
    try:
        if principal <= 0 or annual_rate < 0 or years <= 0:
            logger.error("❌ Invalid inputs: principal=%s, annual_rate=%s, years=%s", principal, annual_rate, years)
            raise ValueError("Principal and years must be positive, rate non-negative")

        if annual_rate > 500 or years > 50:
            logger.error("❌ Out-of-range inputs: annual_rate=%s, years=%s", annual_rate, years)
            raise ValueError("Annual rate must be between 0 and 500%, and years must be between 1 and 50")

        # issue #50: principal had no upper bound, unlike annual_rate/years
        # (fixed for #49). A principal at or beyond Python's float max
        # (~1.8e308) is parsed as `inf` before it ever reaches this
        # function, and even a "merely" astronomical-but-finite principal
        # (e.g. 1e18) produces a technically-200 but nonsensical result.
        # `inf` in particular serializes to the bare token `Infinity` in the
        # JSON response body, which is not valid JSON per spec - browser
        # `JSON.parse`/`response.json()` throws a SyntaxError on it, so the
        # frontend crashes trying to read a "successful" 200 response.
        # Bounding principal the same way rate/years are bounded turns that
        # into a clean, client-safe 400 instead.
        if principal > 1_000_000_000_000:
            logger.error("❌ Out-of-range input: principal=%s", principal)
            raise ValueError("Principal must be between 0 and 1,000,000,000,000")

        monthly_rate = annual_rate / 100 / 12
        months = years * 12
        if monthly_rate == 0:
            monthly_payment = principal / months
            total_interest = 0
        else:
            growth = (1 + monthly_rate) ** months
            monthly_payment = principal * (monthly_rate * growth) / (growth - 1)
            total_interest = (monthly_payment * months) - principal
        
        logger.debug("✅ Loan calculated: monthly_payment=%s, total_interest=%s", monthly_payment, total_interest)
        return {
            "monthly_payment": round(monthly_payment, 2),
            "total_interest": round(total_interest, 2)
        }
    except Exception as e:
        logger.exception("💥 Error calculating loan: %s", str(e))
        raise