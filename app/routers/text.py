from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.models.text import TextRequest, TextResponse, GrammarResponse
from app.core.security import verify_api_key
from app.core.config import settings
from app.services.files.service import UploadValidationError, get_file_by_id, save_text_input
from app.services.jobs.service import create_job, mark_failed, mark_queued
from app.services.text.grammar import correct_grammar
from app.services.text.count import word_count, char_count, sentence_count, paragraph_count
from app.shared.responses import api_error, envelope
import logging
import json

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/text", tags=["Text Tools"])


class TextUploadRequest(BaseModel):
    text: str


class FileIdRequest(BaseModel):
    file_id: str


@router.get("/test", summary="Test Text Tools endpoint", response_model=TextResponse)
async def test_text(api_key: dict = Depends(verify_api_key)):
    logger.debug("🧪 Testing Text Tools endpoint")
    try:
        response = TextResponse(result={"message": "Text Tools router is working"})
        logger.debug("✅ Test endpoint response: %s", json.dumps(response.dict()))
        return response
    except Exception as e:
        logger.exception("💥 Error in test endpoint: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error in test endpoint: {str(e)}")

@router.post("/upload", summary="Upload text input for paraphrase/summarize jobs")
async def upload_text(payload: TextUploadRequest, api_key: dict = Depends(verify_api_key)):
    """Tier 1 - writes `text` to disk and registers a `files` record for it,
    the same way `app/routers/pdf.py`'s `upload_pdf` does for a real upload,
    so `POST /text/paraphrase`/`POST /text/summarize` can reference it by
    `file_id` (Handbook Part C.3/C.5).
    """
    try:
        owner_id = ObjectId(api_key["key_data"]["_id"])
        file_doc = await save_text_input(payload.text, owner_id, "text_input.txt")
    except UploadValidationError as e:
        logger.warning("Text upload rejected: %s", e.message)
        raise api_error(e.status_code, e.message, e.error_code)
    except Exception as e:
        logger.exception("Text upload failed: %s", str(e))
        raise api_error(500, "Failed to upload text", "UPLOAD_FAILED")

    logger.info("Text uploaded: id=%s", file_doc.id)
    return envelope(True, "Text uploaded", data={"file_id": str(file_doc.id), "filename": file_doc.originalFilename})


async def _create_text_job(request: Request, file_id: str, job_type: str, api_key: dict) -> dict:
    """Mirrors `app/routers/pdf.py`'s `_create_pdf_job` / `app/routers/image.py`'s
    `_create_image_job` (Handbook Part C.4): ownership check, create the Job,
    enqueue the matching ARQ task, transition Pending -> Queued.
    """
    file_doc = await get_file_by_id(file_id)
    if file_doc is None:
        raise api_error(404, "File not found or has expired", "FILE_NOT_FOUND")

    owner_id = str(api_key["key_data"]["_id"])
    if str(file_doc.ownerApiKeyId) != owner_id:
        raise api_error(403, "Not authorized to use this file", "FILE_FORBIDDEN")

    job = await create_job(file_doc.id, job_type)
    try:
        await request.app.state.arq_redis.enqueue_job(job_type, str(job.id), _job_id=str(job.id))
        await mark_queued(str(job.id))
    except Exception as e:
        logger.exception("Failed to enqueue job %s (%s): %s", job.id, job_type, str(e))
        await mark_failed(str(job.id), "Failed to queue job for processing")
        raise api_error(503, "Job queue is temporarily unavailable", "QUEUE_UNAVAILABLE")

    logger.info("Created job %s (%s) for file %s", job.id, job_type, file_id)
    return {"job_id": str(job.id), "status": "queued"}


@router.post("/paraphrase", summary="Paraphrase text using AI (async job)")
async def paraphrase(payload: FileIdRequest, request: Request, api_key: dict = Depends(verify_api_key)):
    data = await _create_text_job(request, payload.file_id, "text_paraphrase", api_key)
    return envelope(True, "Paraphrase job created", data=data)


@router.post("/summarize", summary="Summarize text using AI (async job)")
async def summarize(payload: FileIdRequest, request: Request, api_key: dict = Depends(verify_api_key)):
    data = await _create_text_job(request, payload.file_id, "text_summarize", api_key)
    return envelope(True, "Summarization job created", data=data)

@router.post(
    "/grammar",
    summary="Correct grammar in text",
    response_model=GrammarResponse,
    responses={
        200: {"description": "Grammar corrections returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def grammar(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Correcting grammar for text: %s", request.text)
    try:
        result = await correct_grammar(request.text)
        logger.debug("✅ Grammar corrections: %s", result["corrections"])
        return GrammarResponse(**result)
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error correcting grammar: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error correcting grammar: {str(e)}")

@router.post(
    "/word_count",
    summary="Count words in text",
    response_model=TextResponse,
    responses={
        200: {"description": "Word count returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def word_count_endpoint(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔢 Counting words in text: %s", request.text)
    try:
        count = await word_count(request.text)
        logger.debug("✅ Word count: %d", count)
        return TextResponse(text=request.text, result={"word_count": count})
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error counting words: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error counting words: {str(e)}")

@router.post(
    "/char_count",
    summary="Count characters in text",
    response_model=TextResponse,
    responses={
        200: {"description": "Character count returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def char_count_endpoint(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔢 Counting characters in text: %s", request.text)
    try:
        count = await char_count(request.text)
        logger.debug("✅ Character count: %d", count)
        return TextResponse(text=request.text, result={"char_count": count})
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error counting characters: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error counting characters: {str(e)}")

@router.post(
    "/sentence_count",
    summary="Count sentences in text",
    response_model=TextResponse,
    responses={
        200: {"description": "Sentence count returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def sentence_count_endpoint(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔢 Counting sentences in text: %s", request.text)
    try:
        count = await sentence_count(request.text)
        logger.debug("✅ Sentence count: %d", count)
        return TextResponse(text=request.text, result={"sentence_count": count})
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error counting sentences: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error counting sentences: {str(e)}")

@router.post(
    "/paragraph_count",
    summary="Count paragraphs in text",
    response_model=TextResponse,
    responses={
        200: {"description": "Paragraph count returned"},
        400: {"description": "Invalid input"},
        500: {"description": "Server error"}
    }
)
async def paragraph_count_endpoint(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔢 Counting paragraphs in text: %s", request.text)
    try:
        count = await paragraph_count(request.text)
        logger.debug("✅ Paragraph count: %d", count)
        return TextResponse(text=request.text, result={"paragraph_count": count})
    except ValueError as e:
        logger.error("❌ Invalid input: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error counting paragraphs: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Error counting paragraphs: {str(e)}")