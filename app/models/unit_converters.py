from pydantic import BaseModel


class LengthConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class ContextualConvertRequest(BaseModel):
    query: str  # e.g., "convert 5 feet to meters"
