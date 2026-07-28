"""Structural + mocked-model tests for `app.worker`'s `on_startup`/
`on_shutdown` hooks (ADR-015 Open Item 2 - model-loading migration): the
`facebook/bart-large-cnn` summarization pipeline used to be loaded
eagerly, at import time of `app.services.pdf.summarize`; it's now loaded
once per ARQ worker process inside `on_startup(ctx)`
(`WorkerSettings.on_startup`), the worker-process analogue of
`app.state` in `app/main.py`, and released in `on_shutdown(ctx)`.

Downloading/running the real `bart-large-cnn` model in a unit test is
expensive (large model download) - not something this checkout's existing
convention does either (see `tests/test_image_processor.py`'s docstring:
`extract_text_from_image` itself is monkeypatched rather than invoking real
Tesseract, and `tests/test_worker_retry*.py` monkeypatch the tool-specific
extraction/OCR functions rather than exercising real heavy dependencies).
Matching that convention, `transformers.pipeline` itself is monkeypatched
here so this test proves the *wiring* (import site, ctx key, call args,
error handling, cleanup) without ever downloading or running the real model.

IMPORTANT environment note: `transformers`/`torch` are listed in
`requirements.txt` but are **not actually installed** in this checkout's
`.venv` (confirmed separately - `import transformers` raises
`ModuleNotFoundError` here, even though `on_startup()`'s live behavior can
never be exercised in this environment at all, mocked or real). Because of
that, `monkeypatch.setattr("transformers.pipeline", ...)` (the usual
dotted-path form, used e.g. by `tests/test_worker_retry.py` for
`app.services.pdf.processors.extract_text_from_pdf`) can't be used here - it
imports the real module first to resolve the attribute, which fails before
the patch is even applied. Instead this module injects a *fake* `transformers`
module directly into `sys.modules` before calling `on_startup()`, which
`on_startup()`'s own `from transformers import pipeline` then picks up -
this works whether or not the real package is installed, but it does mean
this suite has never imported the real `transformers` package even once.
"""
import sys
from types import SimpleNamespace

import pytest

import app.worker as worker


def _install_fake_transformers_module(monkeypatch, pipeline_fn):
    """Injects `sys.modules["transformers"]` with just a `pipeline` attribute
    - see module docstring for why this is done via `sys.modules` rather
    than `monkeypatch.setattr("transformers.pipeline", ...)`."""
    fake_module = SimpleNamespace(pipeline=pipeline_fn)
    monkeypatch.setitem(sys.modules, "transformers", fake_module)


def test_worker_settings_wires_on_startup_and_on_shutdown_hooks():
    """Purely structural: `WorkerSettings` (what `arq app.worker.WorkerSettings`
    actually reads) must reference the same `on_startup`/`on_shutdown`
    functions this module defines, and register all four job task functions -
    catches the class body drifting out of sync with the module-level
    functions without needing to run anything."""
    assert worker.WorkerSettings.on_startup is worker.on_startup
    assert worker.WorkerSettings.on_shutdown is worker.on_shutdown
    assert worker.WorkerSettings.functions == [
        worker.pdf_convert,
        worker.pdf_to_word,
        worker.pdf_summarize,
        worker.image_ocr,
    ]
    assert worker.WorkerSettings.max_tries == worker.MAX_TRIES


@pytest.mark.asyncio(loop_scope="session")
async def test_on_startup_loads_pipeline_once_and_stores_it_on_ctx(monkeypatch):
    calls = []

    def _fake_pipeline(task, model, device="cpu"):
        calls.append({"task": task, "model": model, "device": device})
        return SimpleNamespace(model="fake-bart-large-cnn-model")

    _install_fake_transformers_module(monkeypatch, _fake_pipeline)

    ctx: dict = {}
    await worker.on_startup(ctx)

    assert len(calls) == 1, "on_startup must load the pipeline exactly once per call"
    assert calls[0] == {"task": "summarization", "model": "facebook/bart-large-cnn", "device": "cpu"}
    assert "summarizer_pipeline" in ctx
    assert ctx["summarizer_pipeline"].model == "fake-bart-large-cnn-model"


@pytest.mark.asyncio(loop_scope="session")
async def test_on_startup_raises_if_pipeline_load_throws(monkeypatch):
    def _broken_pipeline(task, model, device="cpu"):
        raise RuntimeError("simulated model download failure")

    _install_fake_transformers_module(monkeypatch, _broken_pipeline)

    ctx: dict = {}
    with pytest.raises(RuntimeError, match="simulated model download failure"):
        await worker.on_startup(ctx)
    assert "summarizer_pipeline" not in ctx


@pytest.mark.asyncio(loop_scope="session")
async def test_on_startup_raises_if_pipeline_is_invalid_object(monkeypatch):
    """`on_startup` verifies the loaded object looks like a real pipeline
    (`hasattr(pl, "model")`) before trusting it, mirroring `app/main.py`'s
    `_load_pipeline` helper's own verification step."""

    def _fake_pipeline_no_model_attr(task, model, device="cpu"):
        return object()  # no `.model` attribute

    _install_fake_transformers_module(monkeypatch, _fake_pipeline_no_model_attr)

    ctx: dict = {}
    with pytest.raises(ValueError, match="Invalid summarizer_pipeline"):
        await worker.on_startup(ctx)
    assert "summarizer_pipeline" not in ctx


@pytest.mark.asyncio(loop_scope="session")
async def test_on_shutdown_removes_pipeline_from_ctx():
    ctx = {"summarizer_pipeline": object(), "job_try": 1}
    await worker.on_shutdown(ctx)
    assert "summarizer_pipeline" not in ctx
    assert ctx["job_try"] == 1, "on_shutdown must only remove its own key, nothing else in ctx"


@pytest.mark.asyncio(loop_scope="session")
async def test_on_shutdown_is_a_noop_when_pipeline_was_never_loaded():
    """E.g. `on_startup` failed and raised before setting the key - shutdown
    must not itself raise (KeyError) when called."""
    ctx: dict = {"job_try": 1}
    await worker.on_shutdown(ctx)
    assert "summarizer_pipeline" not in ctx


def test_transformers_import_is_not_at_module_scope():
    """Importing `app.worker` must stay light (per the module's own
    docstring) - `transformers` must not already be a bound name at
    `app.worker` module scope; it's only imported inside `on_startup()`."""
    assert "transformers" not in vars(worker)
