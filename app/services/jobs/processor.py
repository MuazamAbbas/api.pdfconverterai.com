"""Shared Processing Engine interface (Handbook Part C.4, ADR-003).

Every Tier 2 tool is Validate -> Prepare -> Execute -> Verify -> Complete.
Concrete processors (e.g. `app.services.pdf.processors.PdfConvertProcessor`)
implement the five steps; `Processor.run()` is the one orchestrator every
job task function in `app/worker.py` calls - it never re-implements this
sequencing itself.

Retry classification (ADR-003): raise `TransientProcessingError` for
temporary faults (I/O hiccup, worker restart mid-job) - the calling task
function retries these, up to `JobDocument.maxRetries`. Raise
`PermanentProcessingError` for corrupted input / unsupported formats -
these fail immediately, never retried.
"""
from typing import Any


class TransientProcessingError(Exception):
    """A temporary fault. Safe to retry (ADR-003)."""


class PermanentProcessingError(Exception):
    """Corrupted input or unsupported format. Never retry (ADR-003)."""


class Processor:
    """Base class for the Validate/Prepare/Execute/Verify/Cleanup pipeline.

    Subclasses override whichever steps they need; steps that don't apply
    can be left at their no-op defaults.
    """

    async def validate(self, job: Any, file_doc: Any) -> None:
        """Raise Transient/PermanentProcessingError if the job can't run at all."""
        return None

    async def prepare(self, job: Any, file_doc: Any) -> dict[str, Any]:
        """Stage anything `execute()` needs (paths, temp dirs). Returned dict is passed through."""
        return {}

    async def execute(self, job: Any, file_doc: Any, prepared: dict[str, Any]) -> dict[str, Any]:
        """Do the actual work. Must be overridden."""
        raise NotImplementedError

    async def verify(self, job: Any, file_doc: Any, result: dict[str, Any]) -> None:
        """Sanity-check `execute()`'s result; raise if it's unusable."""
        return None

    async def cleanup(self, job: Any, file_doc: Any, prepared: dict[str, Any]) -> None:
        """Release anything `prepare()`/`execute()` created. Always runs, success or failure."""
        return None

    async def run(self, job: Any, file_doc: Any) -> dict[str, Any]:
        prepared: dict[str, Any] = {}
        try:
            await self.validate(job, file_doc)
            prepared = await self.prepare(job, file_doc)
            result = await self.execute(job, file_doc, prepared)
            await self.verify(job, file_doc, result)
            return result
        finally:
            await self.cleanup(job, file_doc, prepared)
