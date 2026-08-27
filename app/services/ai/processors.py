"""Concrete Processors for the `ai` module's Tier 2 jobs (Handbook Part C.4,
ADR-003, ADR-018). Implements the shared Validate/Prepare/Execute/
Verify/Cleanup interface from `app/services/jobs/processor.py`;
`app/worker.py`'s `ai_keyword_research`/`ai_content_idea_generator` task
functions are thin ARQ-facing wrappers that call `.run()` and own the Job's
Pending/Queued/Processing/Completed/Failed transitions - mirrors
`app/services/text/processors.py`/`app/services/pdf/processors.py`.

`ai` depends on `jobs` here (imports its Processor base), matching the
Handbook Part C.3 one-direction dependency chain (... file -> job ->
pdf/image/text/ai) - `jobs` never imports anything from `ai`.

This is the first module with Processors filed under `app/services/ai/` -
Keyword Research (ADR-018) is the first Tier 2 AI tool, Content Idea
Generator (ADR-018's third pilot, same Tier 2/module placement, same
Job-System shape) the second; Grammar Checker
(`app/services/ai/grammar_checker.py`) stays Tier 1 (single bounded
LanguageTool call, no job queue).
"""
import logging
import os

from app.services.jobs.processor import (
    PermanentProcessingError,
    Processor,
    TransientProcessingError,
)

logger = logging.getLogger(__name__)


def _read_seed_keyword_input(file_doc) -> str:
    """Read the small seed-keyword text file this job processes.

    Same shape as `app/services/text/processors.py::_read_text_input` -
    the file backing an `ai_keyword_research` job is always a small UTF-8
    text file written by `save_text_input`
    (`app/services/files/service.py`, via
    `app/routers/ai_tools.py::upload_keyword_research_seed`), never a real
    binary upload - read directly as text, not binary.
    """
    if not os.path.exists(file_doc.storagePath):
        raise PermanentProcessingError("Source file is missing or has expired")
    with open(file_doc.storagePath, "r", encoding="utf-8") as f:
        return f.read()


class KeywordResearchProcessor(Processor):
    """job.type == "ai_keyword_research" """

    async def validate(self, job, file_doc):
        seed_keyword = _read_seed_keyword_input(file_doc)
        if not seed_keyword or not seed_keyword.strip():
            raise PermanentProcessingError("Seed keyword must be a non-empty string")

    async def prepare(self, job, file_doc):
        return {"seed_keyword": _read_seed_keyword_input(file_doc)}

    async def execute(self, job, file_doc, prepared, ctx=None):
        from app.services.ai.keyword_research import (
            KeywordResearchUnavailableError,
            research_keywords,
        )

        try:
            result = await research_keywords(prepared["seed_keyword"])
        except ValueError as e:
            # Fixed, hardcoded message rather than `str(e)` - decouples this
            # client-facing contract from `research_keywords`'s text
            # (same convention as issue #39 / `TextParaphraseProcessor` /
            # `TextSummarizeProcessor` in `app/services/text/processors.py`).
            raise PermanentProcessingError("Seed keyword must be a valid, non-empty string") from e
        except KeywordResearchUnavailableError as e:
            # A clean "service unavailable" case (OpenRouter down, circuit
            # breaker tripped, or unparseable model response) - not
            # something a retry within this job's short lifetime will fix,
            # so this fails permanently rather than retrying, mirroring how
            # `app/routers/ai_tools.py::grammar_checker` translates
            # `GrammarCheckerUnavailableError` into a client-facing 503:
            # same intent, landing on the job-failure path instead of an
            # HTTP response (`KeywordResearchUnavailableError`'s own
            # docstring documents this exact expectation).
            raise PermanentProcessingError(
                "Keyword research is temporarily unavailable, please try again shortly"
            ) from e
        except Exception as e:
            raise TransientProcessingError("Temporary error researching keywords") from e
        return result

    async def verify(self, job, file_doc, result):
        if not result.get("suggestions"):
            raise PermanentProcessingError("Keyword research produced no suggestions")


def _read_topic_input(file_doc) -> str:
    """Read the small topic text file this job processes.

    Same shape as `_read_seed_keyword_input` above and
    `app/services/text/processors.py::_read_text_input` - the file backing
    an `ai_content_idea_generator` job is always a small UTF-8 text file
    written by `save_text_input`
    (`app/services/files/service.py`, via
    `app/routers/ai_tools.py::upload_content_idea_generator_topic`), never
    a real binary upload - read directly as text, not binary.
    """
    if not os.path.exists(file_doc.storagePath):
        raise PermanentProcessingError("Source file is missing or has expired")
    with open(file_doc.storagePath, "r", encoding="utf-8") as f:
        return f.read()


class ContentIdeaGeneratorProcessor(Processor):
    """job.type == "ai_content_idea_generator" """

    async def validate(self, job, file_doc):
        topic = _read_topic_input(file_doc)
        if not topic or not topic.strip():
            raise PermanentProcessingError("Topic must be a non-empty string")

    async def prepare(self, job, file_doc):
        return {"topic": _read_topic_input(file_doc)}

    async def execute(self, job, file_doc, prepared, ctx=None):
        from app.services.ai.content_idea_generator import (
            ContentIdeaGeneratorUnavailableError,
            generate_content_ideas,
        )

        try:
            result = await generate_content_ideas(prepared["topic"])
        except ValueError as e:
            # Fixed, hardcoded message rather than `str(e)` - decouples this
            # client-facing contract from `generate_content_ideas`'s text
            # (same convention as `KeywordResearchProcessor.execute` above).
            raise PermanentProcessingError("Topic must be a valid, non-empty string") from e
        except ContentIdeaGeneratorUnavailableError as e:
            # A clean "service unavailable" case (OpenRouter down, circuit
            # breaker tripped, or unparseable model response) - not
            # something a retry within this job's short lifetime will fix,
            # so this fails permanently rather than retrying, mirroring
            # `KeywordResearchProcessor.execute`'s equivalent handling
            # (`ContentIdeaGeneratorUnavailableError`'s own docstring
            # documents this exact expectation).
            raise PermanentProcessingError(
                "Content idea generation is temporarily unavailable, please try again shortly"
            ) from e
        except Exception as e:
            raise TransientProcessingError("Temporary error generating content ideas") from e
        return result

    async def verify(self, job, file_doc, result):
        if not (result.get("blogTitles") and result.get("videoConcepts") and result.get("socialPosts")):
            raise PermanentProcessingError("Content idea generation produced an incomplete result")
