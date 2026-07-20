import os
import aiofiles
from fastapi import UploadFile
from uuid import uuid4
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

STORAGE_PATH = "/tmp/pdfconverterai"
os.makedirs(STORAGE_PATH, exist_ok=True)

async def save_file(file: UploadFile) -> str:
    file_id = str(uuid4())
    file_path = os.path.join(STORAGE_PATH, f"{file_id}-{file.filename}")
    async with aiofiles.open(file_path, "wb") as out_file:
        content = await file.read()
        await out_file.write(content)
    return file_path

def cleanup_old_files():
    now = datetime.utcnow()
    for filename in os.listdir(STORAGE_PATH):
        file_path = os.path.join(STORAGE_PATH, filename)
        file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
        if now - file_mtime > timedelta(hours=24):
            os.remove(file_path)

# Schedule cleanup
scheduler = AsyncIOScheduler()
scheduler.add_job(cleanup_old_files, "interval", hours=1)
scheduler.start()