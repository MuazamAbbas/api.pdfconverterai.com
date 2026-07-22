import logging
from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

try:
    logger.debug(f"Connecting to MongoDB with URL: {settings.database_url}")
    client = AsyncIOMotorClient(settings.database_url)
    db = client["pdfconverterai"]
except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {str(e)}")
    raise

async def get_db():
    try:
        yield db
    except Exception as e:
        logger.error(f"Error in get_db: {str(e)}")
        raise
    finally:
        pass  # Motor handles connection cleanup