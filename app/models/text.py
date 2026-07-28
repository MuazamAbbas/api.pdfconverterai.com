from pydantic import BaseModel, Field
from typing import Optional, Union, List, Dict

class TextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000)

class TextResponse(BaseModel):
    result: Dict[str, Union[str, int, float, dict]]

class GrammarResponse(BaseModel):
    corrections: List[Dict[str, str]]
