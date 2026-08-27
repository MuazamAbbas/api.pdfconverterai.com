import logging

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.security import verify_api_key
from app.services.ai.grammar_checker import (
    MAX_TEXT_LENGTH,
    GrammarCheckerUnavailableError,
    check_grammar,
)
from app.services.ai.keyword_research import MAX_SEED_KEYWORD_LENGTH
from app.services.ai.usage_limits import check_and_increment_ai_tools_daily_usage
from app.services.ai_tools.sentiment import analyze_sentiment_service
from app.services.files.service import UploadValidationError, get_file_by_id, save_text_input
from app.services.jobs.service import create_job, mark_failed, mark_queued
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai_tools", tags=["AI Tools"])

# ARQ job type string for the Keyword Research Tier 2 processor. The worker
# task with this exact name is added by a separate worker-wiring task - this
# router only needs to enqueue it by string, same as
# `app/routers/text.py`'s `_create_text_job`.
KEYWORD_RESEARCH_JOB_TYPE = "ai_keyword_research"

class TextRequest(BaseModel):
    text: str


class GrammarCheckerRequest(BaseModel):
    # A generous outer ceiling (not the exact MAX_TEXT_LENGTH business rule)
    # so a wildly oversized body is rejected by Pydantic before it's fully
    # parsed, without changing the precise "at most 20000 chars" 400 that
    # `_validate_text` (checked below) already owns and existing tests
    # assert on.
    text: str = Field(..., max_length=MAX_TEXT_LENGTH * 5)
    language: str = "en-US"


class KeywordResearchUploadRequest(BaseModel):
    # Same "generous outer ceiling, precise business rule lives in the
    # service layer" pattern as GrammarCheckerRequest.text above.
    seedKeyword: str = Field(..., max_length=MAX_SEED_KEYWORD_LENGTH * 5)


class KeywordResearchRequest(BaseModel):
    # Mirrors app/routers/text.py's FileIdRequest. No `model` field, ever -
    # ADR-018 decision 2: no client-supplied model picker.
    file_id: str

@router.get("/test", summary="Test AI Tools endpoint")
async def test_ai_tools(api_key: dict = Depends(verify_api_key)):
    logger.debug("🧪 Testing AI Tools endpoint")
    return {"message": "AI Tools router is working"}


@router.post("/sentiment", summary="Analyze sentiment of text")
async def sentiment_analysis(request: TextRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Analyzing sentiment for text: %s", request.text)
    try:
        result = await analyze_sentiment_service(request.text)
        logger.debug("✅ Sentiment analysis completed: %s", result)
        return result
    except ValueError as e:
        logger.error("❌ Validation error: %s", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("💥 Error analyzing sentiment: %s", str(e))
        raise api_error(500, "Failed to analyze sentiment", "SENTIMENT_ANALYSIS_FAILED")


@router.post("/grammar_checker", summary="Check grammar via the LanguageTool API")
async def grammar_checker(request: GrammarCheckerRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug(
        "🔍 Checking grammar for text (%d chars, language=%s)",
        len(request.text or ""),
        request.language,
    )
    try:
        result = await check_grammar(request.text, request.language)
        logger.debug("✅ Grammar check completed: %d issue(s)", len(result["issues"]))
        return result
    except ValueError as e:
        logger.error("❌ Validation error: %s", str(e))
        raise api_error(400, str(e), "GRAMMAR_CHECK_INVALID_INPUT")
    except GrammarCheckerUnavailableError as e:
        logger.error("🚫 LanguageTool API unavailable: %s", str(e))
        raise api_error(
            503,
            "Grammar checking is temporarily unavailable, please try again shortly",
            "GRAMMAR_CHECK_UNAVAILABLE",
        )
    except Exception as e:
        logger.exception("💥 Error checking grammar: %s", str(e))
        raise api_error(500, "Failed to check grammar", "GRAMMAR_CHECK_FAILED")


@router.post(
    "/keyword_research/upload",
    summary="Upload a seed keyword for Keyword Research (async job)",
)
async def upload_keyword_research_seed(
    payload: KeywordResearchUploadRequest, api_key: dict = Depends(verify_api_key)
):
    """Tier 2 upload step. Mirrors `app/routers/text.py`'s `POST /text/upload`
    (`save_text_input`) exactly, but owned by `ai_tools` rather than reused
    from `text` - one module = one responsibility (Handbook Part C.3) - since
    the Job System requires a non-optional `fileId` on every job
    (`app/services/jobs/service.py::create_job`) and there's no real file
    upload involved for a short, user-typed seed keyword.
    """
    try:
        owner_id = ObjectId(api_key["key_data"]["_id"])
        file_doc = await save_text_input(payload.seedKeyword, owner_id, "seed_keyword.txt")
    except UploadValidationError as e:
        logger.warning("Keyword research seed upload rejected: %s", e.message)
        raise api_error(e.status_code, e.message, e.error_code)
    except Exception as e:
        logger.exception("Keyword research seed upload failed: %s", str(e))
        raise api_error(500, "Failed to upload seed keyword", "UPLOAD_FAILED")

    logger.info("Keyword research seed uploaded: id=%s", file_doc.id)
    return envelope(
        True,
        "Seed keyword uploaded",
        data={"file_id": str(file_doc.id), "filename": file_doc.originalFilename},
    )


async def _get_owned_keyword_research_file(file_id: str, api_key: dict):
    """Ownership check, mirroring `app/routers/text.py`'s
    `_create_text_job` (lines 61-72 of that file) - adapted for `ai_tools`
    since this router needs the daily-cap check (below) to run between the
    ownership check and job creation, unlike `text`'s single combined
    helper."""
    file_doc = await get_file_by_id(file_id)
    if file_doc is None:
        raise api_error(404, "File not found or has expired", "FILE_NOT_FOUND")

    owner_id = str(api_key["key_data"]["_id"])
    if str(file_doc.ownerApiKeyId) != owner_id:
        raise api_error(403, "Not authorized to use this file", "FILE_FORBIDDEN")
    return file_doc


@router.post(
    "/keyword_research",
    summary="Research related keywords for a seed keyword via OpenRouter (async job)",
)
async def keyword_research(
    payload: KeywordResearchRequest, request: Request, api_key: dict = Depends(verify_api_key)
):
    """Tier 2 (Processing, via Job System) per ADR-018/Handbook Part I.2 -
    LLM generation latency is materially higher/more variable than Grammar
    Checker's single bounded LanguageTool call, so this creates a Job and
    returns immediately; the actual OpenRouter call happens in the ARQ
    worker's `ai_keyword_research` task (added separately - see
    `app/services/ai/keyword_research.py::research_keywords`, the callable
    that task is expected to invoke).

    Enforces the ai_tools-specific daily cap (ADR-018 cost/abuse
    protection) *before* creating the job - separate from, and narrower
    than, the generic per-key `rate_limit_per_day` check `verify_api_key`
    already applies to every request.
    """
    file_doc = await _get_owned_keyword_research_file(payload.file_id, api_key)

    owner_id_str = str(api_key["key_data"]["_id"])
    allowed = await check_and_increment_ai_tools_daily_usage(owner_id_str)
    if not allowed:
        raise api_error(
            429,
            "Daily AI tools request limit reached, try again tomorrow",
            "AI_TOOLS_DAILY_LIMIT_EXCEEDED",
        )

    owner_id = ObjectId(owner_id_str)
    try:
        job = await create_job(file_doc.id, KEYWORD_RESEARCH_JOB_TYPE, owner_id)
    except Exception as e:
        # The daily-cap counter above is already incremented at this point -
        # a rare Mongo hiccup here means the quota is consumed for a job
        # that was never created. Accepted as a low-probability edge case
        # (code-reviewer, PR review pass) rather than blocking merge; still
        # worth wrapping so a transient create_job failure surfaces as a
        # clean 503 instead of an unhandled 500.
        logger.exception("Failed to create keyword research job: %s", str(e))
        raise api_error(503, "Job queue is temporarily unavailable", "QUEUE_UNAVAILABLE")

    try:
        await request.app.state.arq_redis.enqueue_job(
            KEYWORD_RESEARCH_JOB_TYPE, str(job.id), _job_id=str(job.id)
        )
        await mark_queued(str(job.id))
    except Exception as e:
        logger.exception(
            "Failed to enqueue job %s (%s): %s", job.id, KEYWORD_RESEARCH_JOB_TYPE, str(e)
        )
        await mark_failed(str(job.id), "Failed to queue job for processing")
        raise api_error(503, "Job queue is temporarily unavailable", "QUEUE_UNAVAILABLE")

    logger.info(
        "Created job %s (%s) for file %s", job.id, KEYWORD_RESEARCH_JOB_TYPE, payload.file_id
    )
    return envelope(
        True, "Keyword research job created", data={"job_id": str(job.id), "status": "queued"}
    )