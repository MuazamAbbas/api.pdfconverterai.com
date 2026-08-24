from pydantic import BaseModel


class URLEncodeRequest(BaseModel):
    url: str

class WebpageSummarizeRequest(BaseModel):
    url: str

class WhoisLookupRequest(BaseModel):
    domain: str

class IPLookupRequest(BaseModel):
    ip: str

class SpeedTestRequest(BaseModel):
    url: str
