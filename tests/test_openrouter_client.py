"""Tests for `app.services.ai.openrouter_client` - the shared OpenRouter
client + circuit breaker (ADR-018) that `keyword_research.py` (and later
OpenRouter-backed `ai_tools` pilots) sits on top of.

`aiohttp.ClientSession` is faked out the same way
`tests/test_grammar_checker.py` fakes it for `check_grammar()`: a scripted
fake response/session/session-factory patched onto
`openrouter_client_service.aiohttp.ClientSession` via `monkeypatch`, no real
network I/O.

Each test resets the module-level circuit-breaker state directly
(`_consecutive_failures`/`_breaker_tripped_until`) via `monkeypatch`, since
that state is process-global and would otherwise leak between tests.
"""
import asyncio

import aiohttp
import pytest

import app.services.ai.openrouter_client as openrouter_client_service
from app.services.ai.openrouter_client import OpenRouterUnavailableError, call_openrouter

pytestmark = pytest.mark.asyncio(loop_scope="session")


_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


@pytest.fixture(autouse=True)
def _reset_breaker(monkeypatch):
    """Every test starts with a closed (untripped) breaker and a zeroed
    failure counter, regardless of what a previous test left behind."""
    monkeypatch.setattr(openrouter_client_service, "_consecutive_failures", 0)
    monkeypatch.setattr(openrouter_client_service, "_breaker_tripped_until", None)


class _FakeORResponse:
    def __init__(self, status: int, payload: dict | None = None):
        self.status = status
        self._payload = payload or {}

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _ScriptedORSession:
    def __init__(self, responses=None, raise_on_post: Exception | None = None):
        # `responses` may be a single response (reused for every call) or a
        # list consumed in order, for tests that need N scripted calls
        # (e.g. driving the breaker to trip after 3 consecutive failures).
        self._responses = responses
        self._raise_on_post = raise_on_post
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, headers=None, json=None, **kwargs):
        self.calls.append((url, json))
        if self._raise_on_post is not None:
            raise self._raise_on_post
        if isinstance(self._responses, list):
            return self._responses[len(self.calls) - 1]
        return self._responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _ScriptedORSessionFactory:
    def __init__(self, session):
        self._session = session

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


def _patch_session(monkeypatch, session):
    monkeypatch.setattr(
        openrouter_client_service.aiohttp, "ClientSession", _ScriptedORSessionFactory(session)
    )


_OPENROUTER_PAYLOAD = {"choices": [{"message": {"content": "hello from the model"}}]}


# --- happy path --------------------------------------------------------


async def test_call_openrouter_returns_message_content(monkeypatch):
    session = _ScriptedORSession(responses=_FakeORResponse(200, _OPENROUTER_PAYLOAD))
    _patch_session(monkeypatch, session)

    result = await call_openrouter("some prompt")
    assert result == "hello from the model"

    # Used the fixed model, not a caller-supplied one - ADR-018 decision 2.
    url, payload = session.calls[0]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert payload["model"] == openrouter_client_service.OPENROUTER_MODEL
    assert payload["messages"] == [{"role": "user", "content": "some prompt"}]


async def test_call_openrouter_success_resets_failure_counter(monkeypatch):
    monkeypatch.setattr(openrouter_client_service, "_consecutive_failures", 2)
    session = _ScriptedORSession(responses=_FakeORResponse(200, _OPENROUTER_PAYLOAD))
    _patch_session(monkeypatch, session)

    await call_openrouter("some prompt")
    assert openrouter_client_service._consecutive_failures == 0


# --- failure -> OpenRouterUnavailableError, no leaked exception text -----


async def test_call_openrouter_timeout_raises_unavailable_without_leaking_text(monkeypatch):
    session = _ScriptedORSession(raise_on_post=asyncio.TimeoutError(_SECRET_MARKER))
    _patch_session(monkeypatch, session)

    with pytest.raises(OpenRouterUnavailableError) as exc_info:
        await call_openrouter("prompt")
    assert _SECRET_MARKER not in str(exc_info.value)
    assert "TimeoutError" not in str(exc_info.value)


async def test_call_openrouter_connection_error_raises_unavailable(monkeypatch):
    session = _ScriptedORSession(raise_on_post=aiohttp.ClientOSError(_SECRET_MARKER))
    _patch_session(monkeypatch, session)

    with pytest.raises(OpenRouterUnavailableError):
        await call_openrouter("prompt")


async def test_call_openrouter_429_raises_unavailable(monkeypatch):
    session = _ScriptedORSession(responses=_FakeORResponse(429))
    _patch_session(monkeypatch, session)

    with pytest.raises(OpenRouterUnavailableError):
        await call_openrouter("prompt")


async def test_call_openrouter_5xx_raises_unavailable(monkeypatch):
    session = _ScriptedORSession(responses=_FakeORResponse(502))
    _patch_session(monkeypatch, session)

    with pytest.raises(OpenRouterUnavailableError):
        await call_openrouter("prompt")


async def test_call_openrouter_unexpected_response_shape_raises_unavailable(monkeypatch):
    session = _ScriptedORSession(responses=_FakeORResponse(200, {"unexpected": "shape"}))
    _patch_session(monkeypatch, session)

    with pytest.raises(OpenRouterUnavailableError):
        await call_openrouter("prompt")


# --- circuit breaker ------------------------------------------------------


async def test_breaker_trips_after_three_consecutive_failures_and_short_circuits(monkeypatch):
    session = _ScriptedORSession(
        responses=[_FakeORResponse(502), _FakeORResponse(502), _FakeORResponse(502)]
    )
    _patch_session(monkeypatch, session)

    for _ in range(3):
        with pytest.raises(OpenRouterUnavailableError):
            await call_openrouter("prompt")

    assert await openrouter_client_service.is_breaker_tripped() is True
    assert len(session.calls) == 3

    # A 4th call must short-circuit - no additional HTTP call made - even
    # though this session would return success if actually called.
    session_after_trip = _ScriptedORSession(responses=_FakeORResponse(200, _OPENROUTER_PAYLOAD))
    _patch_session(monkeypatch, session_after_trip)

    with pytest.raises(OpenRouterUnavailableError):
        await call_openrouter("prompt")
    assert session_after_trip.calls == []  # never actually called out


async def test_breaker_does_not_trip_after_only_two_consecutive_failures(monkeypatch):
    session = _ScriptedORSession(responses=[_FakeORResponse(502), _FakeORResponse(502)])
    _patch_session(monkeypatch, session)

    for _ in range(2):
        with pytest.raises(OpenRouterUnavailableError):
            await call_openrouter("prompt")

    assert await openrouter_client_service.is_breaker_tripped() is False


async def test_breaker_resets_consecutive_count_on_intervening_success(monkeypatch):
    """Two failures, then a success, then two more failures must NOT trip
    the breaker - the counter should reset to 0 on the success in between,
    per `call_openrouter`'s "any successful call resets the counter"
    behavior."""
    session = _ScriptedORSession(
        responses=[
            _FakeORResponse(502),
            _FakeORResponse(502),
            _FakeORResponse(200, _OPENROUTER_PAYLOAD),
            _FakeORResponse(502),
            _FakeORResponse(502),
        ]
    )
    _patch_session(monkeypatch, session)

    with pytest.raises(OpenRouterUnavailableError):
        await call_openrouter("prompt")
    with pytest.raises(OpenRouterUnavailableError):
        await call_openrouter("prompt")
    await call_openrouter("prompt")  # success - resets the counter
    with pytest.raises(OpenRouterUnavailableError):
        await call_openrouter("prompt")
    with pytest.raises(OpenRouterUnavailableError):
        await call_openrouter("prompt")

    assert await openrouter_client_service.is_breaker_tripped() is False


async def test_breaker_reopens_after_cooldown_expires(monkeypatch):
    """Simulates cooldown expiry by moving `_breaker_tripped_until` into the
    past, rather than sleeping 5 real minutes in a test."""
    monkeypatch.setattr(openrouter_client_service, "_consecutive_failures", 3)
    monkeypatch.setattr(
        openrouter_client_service,
        "_breaker_tripped_until",
        openrouter_client_service.time.monotonic() - 1,
    )
    assert await openrouter_client_service.is_breaker_tripped() is False

    session = _ScriptedORSession(responses=_FakeORResponse(200, _OPENROUTER_PAYLOAD))
    _patch_session(monkeypatch, session)

    result = await call_openrouter("prompt")
    assert result == "hello from the model"
    assert len(session.calls) == 1  # the call actually went out this time
