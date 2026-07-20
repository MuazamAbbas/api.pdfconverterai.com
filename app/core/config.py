from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    app_name: str = "PDFConverterAI"
    app_env: str = "production"
    database_url: str
    allowed_origins: str = "https://pdfconverterai.com,https://api.pdfconverterai.com"
    model_path: str = "/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/models"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()