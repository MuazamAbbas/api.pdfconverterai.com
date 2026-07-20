import logging
from datetime import datetime
import os

def setup_logger():
    logger = logging.getLogger("pdfconverterai")
    logger.setLevel(logging.INFO)
    
    # File handler
    log_file = f"/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/{datetime.now().strftime('%Y-%m-%d')}.log"
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logger()