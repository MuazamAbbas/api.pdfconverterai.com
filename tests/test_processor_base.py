"""Unit tests for `app.services.jobs.processor.Processor.run()` - the shared
Validate/Prepare/Execute/Verify/Cleanup orchestrator every Tier 2 job task
function calls (Handbook Part C.4, ADR-003). Pure unit tests: no Mongo/Redis,
a recording fake Processor subclass only.

Also covers `run()`/`execute()`'s optional `ctx` parameter (ADR-015 Open Item
2, added so `app/worker.py` can hand each processor the ARQ worker-process
context dict, e.g. `ctx["summarizer_pipeline"]` for `PdfSummarizeProcessor`) -
`RecordingProcessor.execute()` below declares `ctx=None` in its signature to
match the convention every real `Processor` subclass now follows
(`app/services/pdf/processors.py`, `app/services/image/processors.py`); a
subclass that omits it breaks with `TypeError: execute() takes N positional
arguments but N+1 were given`, since `Processor.run()` always calls
`self.execute(job, file_doc, prepared, ctx)` positionally, ctx or not.
"""
import pytest

from app.services.jobs.processor import (
    PermanentProcessingError,
    Processor,
    TransientProcessingError,
)

# No Mongo/Redis here, but pinned to the same session-scoped loop as every
# other test module anyway - mixing loop scopes across the suite corrupts
# asyncio's "current event loop" global state for Motor's background
# monitor thread (see tests/test_worker_retry.py's comment for the full
# explanation). Keeping exactly one loop scope suite-wide avoids that.
pytestmark = pytest.mark.asyncio(loop_scope="session")


class RecordingProcessor(Processor):
    def __init__(self, execute_result=None, execute_exc=None):
        self.calls: list[str] = []
        self.last_ctx = None
        self._execute_result = execute_result
        self._execute_exc = execute_exc

    async def validate(self, job, file_doc):
        self.calls.append("validate")

    async def prepare(self, job, file_doc):
        self.calls.append("prepare")
        return {"staged": True}

    async def execute(self, job, file_doc, prepared, ctx=None):
        self.calls.append("execute")
        self.last_ctx = ctx
        assert prepared == {"staged": True}
        if self._execute_exc:
            raise self._execute_exc
        return self._execute_result

    async def verify(self, job, file_doc, result):
        self.calls.append("verify")

    async def cleanup(self, job, file_doc, prepared):
        self.calls.append("cleanup")


class FailingPrepareProcessor(RecordingProcessor):
    async def prepare(self, job, file_doc):
        self.calls.append("prepare")
        raise PermanentProcessingError("prep blew up")


class FailingValidateProcessor(RecordingProcessor):
    async def validate(self, job, file_doc):
        self.calls.append("validate")
        raise PermanentProcessingError("nope")


async def test_run_calls_all_steps_in_order_on_success():
    p = RecordingProcessor(execute_result={"ok": True})
    result = await p.run(job=object(), file_doc=object())
    assert result == {"ok": True}
    assert p.calls == ["validate", "prepare", "execute", "verify", "cleanup"]


async def test_run_defaults_ctx_to_none_when_not_passed():
    """`Processor.run()`'s `ctx` parameter (ADR-015 Open Item 2) is optional -
    callers that don't pass one (e.g. any caller outside `app/worker.py`)
    must still work, with `execute()` receiving `ctx=None`."""
    p = RecordingProcessor(execute_result={"ok": True})
    await p.run(job=object(), file_doc=object())
    assert p.last_ctx is None


async def test_run_threads_ctx_through_to_execute():
    """`app/worker.py`'s `_run_job` calls `processor.run(job, file_doc, ctx)`
    with the real ARQ worker-process ctx dict - `run()` must pass it through
    to `execute()` unchanged (positionally, as its 4th argument) so
    processors like `PdfSummarizeProcessor` can reach
    `ctx["summarizer_pipeline"]`."""
    p = RecordingProcessor(execute_result={"ok": True})
    sentinel_ctx = {"summarizer_pipeline": object()}
    await p.run(job=object(), file_doc=object(), ctx=sentinel_ctx)
    assert p.last_ctx is sentinel_ctx


async def test_run_cleanup_runs_when_execute_raises_permanent_error():
    p = RecordingProcessor(execute_exc=PermanentProcessingError("bad file"))
    with pytest.raises(PermanentProcessingError):
        await p.run(job=object(), file_doc=object())
    # verify() must not run after a failed execute(), but cleanup() must.
    assert p.calls == ["validate", "prepare", "execute", "cleanup"]


async def test_run_cleanup_runs_when_execute_raises_transient_error():
    p = RecordingProcessor(execute_exc=TransientProcessingError("io hiccup"))
    with pytest.raises(TransientProcessingError):
        await p.run(job=object(), file_doc=object())
    assert p.calls == ["validate", "prepare", "execute", "cleanup"]


async def test_run_cleanup_runs_when_verify_rejects_the_result():
    class RejectingVerify(RecordingProcessor):
        async def verify(self, job, file_doc, result):
            self.calls.append("verify")
            raise PermanentProcessingError("result unusable")

    p = RejectingVerify(execute_result={"text": ""})
    with pytest.raises(PermanentProcessingError):
        await p.run(job=object(), file_doc=object())
    assert p.calls == ["validate", "prepare", "execute", "verify", "cleanup"]


async def test_run_short_circuits_on_validate_failure_but_still_calls_cleanup():
    p = FailingValidateProcessor()
    with pytest.raises(PermanentProcessingError):
        await p.run(job=object(), file_doc=object())
    # prepare/execute/verify never run - validate failed - but cleanup()
    # always runs (Processor.run()'s try/finally now wraps validate() too).
    assert p.calls == ["validate", "cleanup"]


async def test_run_calls_cleanup_if_prepare_raises():
    """`Processor.cleanup()`'s docstring says it "Always runs, success or
    failure". `Processor.run()`'s try/finally now wraps
    validate()/prepare()/execute()/verify() so a `prepare()` failure still
    runs `cleanup()`, matching the docstring (previously the try/finally
    only wrapped execute()/verify(), so a prepare() failure skipped
    cleanup() entirely - this test used to document that narrower, wrong
    behavior).
    """
    p = FailingPrepareProcessor()
    with pytest.raises(PermanentProcessingError):
        await p.run(job=object(), file_doc=object())
    assert p.calls == ["validate", "prepare", "cleanup"]
