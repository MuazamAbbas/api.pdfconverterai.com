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
                ("-", 1, opAssoc.RIGHT),  # Unary minus
                ("*", 2, opAssoc.LEFT),
                ("/", 2, opAssoc.LEFT),
                ("+", 2, opAssoc.LEFT),
                ("-", 2, opAssoc.LEFT),
            ]
        )
        parsed = expr.parseString(request.expression, parseAll=True)

        # Recursive function to evaluate ParseResults
        def evaluate(parsed):
            if isinstance(parsed, (int, float)):
                return float(parsed)
            if len(parsed) == 1:
                return evaluate(parsed[0])
            if len(parsed) == 2:  # Unary minus
                return -evaluate(parsed[1])
            left, op, right = evaluate(parsed[0]), parsed[1], evaluate(parsed[2])
            if op == "+":
                return left + right
            if op == "-":
                return left - right
            if op == "*":
                return left * right
            if op == "/":
                if right == 0:
                    raise ValueError("Division by zero")
                return left / right
            raise ValueError(f"Unknown operator: {op}")

        result = evaluate(parsed)
        logger.debug("✅ Expression evaluated: %s = %s", request.expression, result)
        return {"expression": request.expression, "result": result}
    except ParseException as e:
        logger.error("❌ Invalid expression: %s", str(e))
        raise HTTPException(status_code=400, detail=f"Invalid expression: {str(e)}")
    except Exception as e:
        logger.exception("💥 Error evaluating expression: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error evaluating expression: {str(e)}")