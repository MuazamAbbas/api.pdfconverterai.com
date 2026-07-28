from pydantic import BaseModel

class AgeCalculatorRequest(BaseModel):
    birth_date: str  # Format: YYYY-MM-DD

class PercentageCalculatorRequest(BaseModel):
    value: float
    percentage: float

class LoanCalculatorRequest(BaseModel):
    principal: float
    annual_rate: float
    years: int

class BMICalculatorRequest(BaseModel):
    weight_kg: float
    height_m: float

class FinancialPlanRequest(BaseModel):
    principal: float
    rate: float
    years: int
    market_sentiment: str
