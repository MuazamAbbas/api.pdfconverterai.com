from pydantic import BaseModel


class LengthConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class TemperatureConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class WeightConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class AreaConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class VolumeConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class SpeedConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class TimeConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class DataConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class EnergyConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class ContextualConvertRequest(BaseModel):
    query: str  # e.g., "convert 5 feet to meters"
