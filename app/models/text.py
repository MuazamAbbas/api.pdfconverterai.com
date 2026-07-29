from typing import Dict, List, Union

from pydantic import BaseModel, Field


class TextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000)

class TextResponse(BaseModel):
    result: Dict[str, Union[str, int, float, dict]]

class GrammarResponse(BaseModel):
    corrections: List[Dict[str, str]]
