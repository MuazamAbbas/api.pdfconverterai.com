"""Unit tests for `app.core.logging.setup_logging` - the fix for the
error.log-stopped-receiving-writes regression (SPRINT_STATUS.md, 2026-07-29
finding): `logging.basicConfig()` used to be called separately from
`app/main.py`, `app/core/database.py`, `app/core/security.py`, 12 router
modules, and `app/services/text/paraphrase.py`, which is a no-op after the
first call configures the root logger - so whichever module happened to
import first silently "won" and the rest of those calls did nothing. Now
only `setup_logging()` (called once, from the top of `app/main.py` before
any router/service import) is allowed to configure handlers; every other
module just does `logging.getLogger(__name__)`.

These tests assert against `logging.basicConfig`'s call arguments rather
than inspecting the real root logger's handler list: pytest's own built-in
log-capture plugin (`_pytest.logging.LoggingPlugin`) attaches its own
`LogCaptureHandler` to the root logger around every test's "call" phase
regardless of what a fixture does beforehand, which makes `basicConfig()`'s
"only configure if the root logger has no handlers yet" no-op behavior
trigger unconditionally under pytest and silently defeats a test that just
clears+inspects `logging.getLogger().handlers` directly (confirmed
experimentally: `logging.getLogger().handlers == []` outside pytest,
non-empty inside a pytest test body). Asserting on the `basicConfig` call
itself sidesteps that entirely and is exactly what this module's contract
actually is: "call `logging.basicConfig()` once, with a FileHandler at the
configured path and a StreamHandler".
"""
import logging

import pytest

from app.core.logging import setup_logging


def test_setup_logging_configures_root_via_basic_config_with_file_and_stream_handlers(
    monkeypatch, tmp_path,
):
    calls = []
    monkeypatch.setattr(
        "app.core.logging.logging.basicConfig",
        lambda **kwargs: calls.append(kwargs),
    )
    # Use a real, platform-appropriate absolute path (via tmp_path) rather
    # than a hardcoded Windows-style literal - `C:\tmp\error.log` isn't
    # recognized as absolute on POSIX, so FileHandler/os.path.abspath()
    # silently prepends the CWD to it there, breaking the exact-match
    # assertion below on Linux CI runners even though the underlying
    # behavior (use whatever path settings gives it) is correct.
    log_path = str(tmp_path / "error.log")
    monkeypatch.setattr("app.core.config.settings.downloaders_log_path", log_path)
    monkeypatch.setattr("app.core.config.settings.log_level", "INFO")

    setup_logging()

    assert len(calls) == 1, "setup_logging() must call logging.basicConfig() exactly once"
    kwargs = calls[0]
    assert kwargs["level"] == "INFO"

    handlers = kwargs["handlers"]
    assert len(handlers) == 2
    file_handlers = [h for h in handlers if isinstance(h, logging.FileHandler)]
    stream_handlers = [h for h in handlers if type(h) is logging.StreamHandler]
    assert len(file_handlers) == 1, "expected exactly one FileHandler among the configured handlers"
    assert len(stream_handlers) == 1, "expected exactly one plain StreamHandler among the configured handlers"
    assert file_handlers[0].baseFilename == log_path


def test_setup_logging_uses_the_configured_log_path_setting(monkeypatch, tmp_path):
    """`setup_logging()` must read the path from `settings.downloaders_log_path`
    at call time, not hardcode it - this is the setting
    `app/core/database.py`/`app/core/security.py` used to hardcode their own
    (now-deleted) `basicConfig()` calls against."""
    calls = []
    monkeypatch.setattr(
        "app.core.logging.logging.basicConfig",
        lambda **kwargs: calls.append(kwargs),
    )
    custom_path = str(tmp_path / "custom-error.log")
    monkeypatch.setattr("app.core.config.settings.downloaders_log_path", custom_path)

    setup_logging()

    file_handler = next(h for h in calls[0]["handlers"] if isinstance(h, logging.FileHandler))
    assert file_handler.baseFilename == custom_path


def test_setup_logging_does_not_raise_against_a_real_writable_path(tmp_path, monkeypatch):
    """Smoke test with the real (unmocked) `logging.basicConfig` - confirms
    `setup_logging()` itself doesn't blow up constructing a real
    `FileHandler`/`StreamHandler` pair, independent of whatever handlers
    pytest's own log-capture plugin has already attached to the root logger
    (see module docstring: that's why this doesn't also assert on
    `logging.getLogger().handlers` here)."""
    monkeypatch.setattr(
        "app.core.config.settings.downloaders_log_path", str(tmp_path / "error.log")
    )

    setup_logging()  # must not raise


@pytest.fixture
def _reset_root_handlers():
    root = logging.getLogger()
    original = list(root.handlers)
    yield root
    root.handlers = original


def test_setup_logging_real_call_writes_to_file_when_root_has_no_existing_handlers(
    _reset_root_handlers, tmp_path, monkeypatch
):
    """End-to-end version of the above with the real `basicConfig`, run
    against a root logger forced empty first so this test doesn't depend on
    whatever pytest's log-capture plugin happens to have attached - directly
    exercises the actual regression this module fixes: a log call after
    `setup_logging()` must land in the configured file."""
    root = _reset_root_handlers
    root.handlers = []
    log_path = tmp_path / "error.log"
    monkeypatch.setattr("app.core.config.settings.downloaders_log_path", str(log_path))

    setup_logging()
    logging.getLogger("some.module").error("a real log line")
    for h in root.handlers:
        if isinstance(h, logging.FileHandler):
            h.flush()

    assert log_path.exists()
    assert "a real log line" in log_path.read_text()
