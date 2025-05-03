from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from pyparsing import pyparsing_common, infixNotation, opAssoc, ParseException, one_of
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

router = APIRouter(prefix="/calculators", tags=["calculators"])

class CalculateRequest(BaseModel):
    expression: str

@router.get("/test", summary="Test Calculators endpoint")
async def test_calculators():
    logger.debug("🧪 Testing Calculators endpoint")
    return {"message": "Calculators router is working"}

@router.post("/calculate", summary="Evaluate a basic arithmetic expression")
async def calculate(request: CalculateRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Evaluating expression: %s", request.expression)
    try:
        # Define grammar for arithmetic expressions
        number = pyparsing_common.number
        operator = one_of("+ - * /")
        expr = infixNotation(
            number,
            [
                ("-", 1, opAssoc.RIGHT),
                ("+", 2, opAssoc.LEFT),
                ("-", 2, opAssoc.LEFT),
                ("*", 2, opAssoc.LEFT),
                ("/", 2, opAssoc.LEFT),
            ]
        )
        parsed = expr.parseString(request.expression, parseAll=True)
        result = parsed[0]
        logger.debug("✅ Expression evaluated: %s = %s", request.expression, result)
        return {"expression": request.expression, "result": float(result)}
    except ParseException as e:
        logger.error("❌ Invalid expression: %s", str(e))
        raise HTTPException(status_code=400, detail=f"Invalid expression: {str(e)}")
    except Exception as e:
        logger.exception("💥 Error evaluating expression: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error evaluating expression: {str(e)}")