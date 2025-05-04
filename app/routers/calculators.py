from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from datetime import datetime
import logging
from app.core.security import verify_api_key
from app.services.calculators.percentage import calculate_percentage
from app.services.calculators.loan import calculate_loan

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calculators", tags=["calculators"])

class AgeCalculatorRequest(BaseModel):
    birth_date: str  # Format: YYYY-MM-DD

class PercentageCalculatorRequest(BaseModel):
    value: float
    percentage: float

class LoanCalculatorRequest(BaseModel):
    principal: float
    annual_rate: float
    years: int

@router.get("/test", summary="Test Calculators endpoint")
async def test_calculators(api_key: dict = Depends(verify_api_key)):
    logger.debug("🧪 Testing Calculators endpoint")
    return {"message": "Calculators router is working"}

@router.post("/age", summary="Calculate age from birth date")
async def calculate_age(request: AgeCalculatorRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Calculating age for birth date: %s", request.birth_date)
    try:
        # Validate date format
        try:
            birth = datetime.strptime(request.birth_date, "%Y-%m-%d")
        except ValueError as e:
            logger.error("❌ Invalid date format: %s", str(e))
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        
        today = datetime.utcnow()
        logger.debug("📅 Today: %s, Birth: %s", today, birth)
        
        # Check if birth date is in the future
        if birth > today:
            logger.error("❌ Birth date is in the future: %s", request.birth_date)
            raise HTTPException(status_code=400, detail="Birth date cannot be in the future")
        
        # Calculate age
        age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
        logger.debug("✅ Age calculated: %s years", age)
        return {"birth_date": request.birth_date, "age": age}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("💥 Error calculating age: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error calculating age: {str(e)}")

@router.post("/percentage", summary="Calculate percentage of a value")
async def calculate_percentage_endpoint(request: PercentageCalculatorRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Calculating percentage: %s%% of %s", request.percentage, request.value)
    try:
        result = calculate_percentage(request.value, request.percentage)
        logger.debug("✅ Percentage result: %s", result)
        return {"value": request.value, "percentage": request.percentage, "result": result}
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error calculating percentage: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error calculating percentage: {str(e)}")

@router.post("/loan", summary="Calculate loan payment")
async def calculate_loan_endpoint(request: LoanCalculatorRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Calculating loan: principal=%s, rate=%s, years=%s", request.principal, request.annual_rate, request.years)
    try:
        result = calculate_loan(request.principal, request.annual_rate, request.years)
        logger.debug("✅ Loan result: %s", result)
        return result
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error calculating loan: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error calculating loan: {str(e)}")