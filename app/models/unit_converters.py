from pydantic import BaseModel, Field


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

class PowerConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class PressureConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class FrequencyConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class ForceConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class TorqueConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class DensityConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class FlowRateConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class AngleConvertRequest(BaseModel):
    value: float
    from_unit: str
    to_unit: str

class FuelEfficiencyConvertRequest(BaseModel):
    # `allow_inf_nan=False` rejects `inf`/`nan` (including JSON's
    # non-standard bare `Infinity`/`-Infinity`/`NaN` tokens and float
    # overflow like `1e400`) at request-validation time with a clean 422,
    # before the handler's `value == 0` zero-guards ever run - `inf == 0`
    # and `nan == 0` are both `False`, so without this constraint such a
    # value would sail past those guards and fail later at response
    # serialization with a generic 500 instead.
    value: float = Field(allow_inf_nan=False)
    from_unit: str
    to_unit: str

class ContextualConvertRequest(BaseModel):
    query: str  # e.g., "convert 5 feet to meters"
