from pydantic import BaseModel

class URLEncodeRequest(BaseModel):
    url: str

class WebpageSummarizeRequest(BaseModel):
    url: str
