"""Tests for `app.services.ai.usage_limits.check_and_increment_ai_tools_daily_usage`
- the `ai_tools`-specific daily-cap sub-budget (ADR-018), separate from the
generic per-key `rate_limit_per_day` check in `app/core/security.py`.

Against the real local Mongo (`mongodb://localhost:27017`, db
`pdfconverterai`), same convention as `tests/conftest.py` - no mocking of
the DB layer, per the DB-access convention used throughout this test suite.
HTTP-level daily-cap enforcement (the 429 wired into the router) is covered
separately in `tests/test_files_jobs_keyword_research_flow.py`; this file
is scoped to the service function itself.
"""
import os

os.environ.setdefault("DATABASE_URL", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

import uuid

import pytest

from app.core.database import db
from app.services.ai.usage_limits import (
    AI_TOOLS_DAILY_LIMIT,
    check_and_increment_ai_tools_daily_usage,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def fake_api_key_id():
    return f"test-ai-tools-usage-{uuid.uuid4()}"


@pytest.fixture(autouse=True)
async def _cleanup(fake_api_key_id):
    yield
    await db.ai_tools_usage.delete_many({"apiKeyId": fake_api_key_id})


async def test_first_request_of_the_day_is_allowed(fake_api_key_id):
    allowed = await check_and_increment_ai_tools_daily_usage(fake_api_key_id)
    assert allowed is True

    doc = await db.ai_tools_usage.find_one({"apiKeyId": fake_api_key_id})
    assert doc["count"] == 1


async def test_requests_up_to_the_limit_are_all_allowed(fake_api_key_id):
    for _ in range(AI_TOOLS_DAILY_LIMIT):
        allowed = await check_and_increment_ai_tools_daily_usage(fake_api_key_id)
        assert allowed is True


async def test_request_over_the_limit_is_rejected(fake_api_key_id):
    for _ in range(AI_TOOLS_DAILY_LIMIT):
        assert await check_and_increment_ai_tools_daily_usage(fake_api_key_id) is True

    over_limit = await check_and_increment_ai_tools_daily_usage(fake_api_key_id)
    assert over_limit is False

    # The rejected request is still counted (matches the existing
    # usage_count pattern in app/core/security.py).
    doc = await db.ai_tools_usage.find_one({"apiKeyId": fake_api_key_id})
    assert doc["count"] == AI_TOOLS_DAILY_LIMIT + 1


async def test_usage_is_scoped_per_api_key(fake_api_key_id):
    other_key_id = f"test-ai-tools-usage-other-{uuid.uuid4()}"
    try:
        for _ in range(AI_TOOLS_DAILY_LIMIT):
            assert await check_and_increment_ai_tools_daily_usage(fake_api_key_id) is True
        assert await check_and_increment_ai_tools_daily_usage(fake_api_key_id) is False

        # A different key's budget is untouched by the first key's usage.
        assert await check_and_increment_ai_tools_daily_usage(other_key_id) is True
    finally:
        await db.ai_tools_usage.delete_many({"apiKeyId": other_key_id})


async def test_usage_is_scoped_per_day(fake_api_key_id, monkeypatch):
    import datetime as datetime_module

    import app.services.ai.usage_limits as usage_limits_service

    class _FixedDate(datetime_module.date):
        @classmethod
        def today(cls):
            return cls(2026, 1, 1)

    monkeypatch.setattr(usage_limits_service, "date", _FixedDate)
    for _ in range(AI_TOOLS_DAILY_LIMIT):
        assert await check_and_increment_ai_tools_daily_usage(fake_api_key_id) is True
    assert await check_and_increment_ai_tools_daily_usage(fake_api_key_id) is False

    monkeypatch.setattr(
        usage_limits_service,
        "date",
        type("_NextDay", (datetime_module.date,), {"today": classmethod(lambda cls: cls(2026, 1, 2))}),
    )
    # A new day resets the budget even for the same key.
    assert await check_and_increment_ai_tools_daily_usage(fake_api_key_id) is True

    await db.ai_tools_usage.delete_many(
        {"apiKeyId": fake_api_key_id, "date": {"$in": ["2026-01-01", "2026-01-02"]}}
    )
