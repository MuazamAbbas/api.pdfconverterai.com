"""Unit tests for `app.services.calculators.financial_plan.FinancialPlanCalculator`
(ADR-015 Open Item 2 - model-loading migration): it used to be a bare
module-level `sentiment_analyzer = pipeline(...)` plus a free function; now
it's a class that reads `self.app.state.financial_sentiment_pipeline`,
mirroring `app.services.text.summarize.Summarizer` /
`app.services.web_tools.summarize.Summarizer`'s existing `self.app.state.*`
convention (both of those also aren't covered by real-model tests in
`tests/`, so a fake `app.state` stub here matches this repo's actual
convention for this class of test, not just a judgment call).

Pure unit tests: no Mongo/Redis, no real `distilbert-base-uncased-
finetuned-sst-2-english` model - `app` is a `SimpleNamespace(state=...)`
stub whose `financial_sentiment_pipeline` is a plain callable returning a
canned sentiment-analysis-pipeline-shaped result
(`[{"label": ..., "score": ...}]`), matching what
`app/main.py::_load_pipeline`'s real Hugging Face `sentiment-analysis`
pipeline returns when called.
"""
from types import SimpleNamespace

import pytest

from app.services.calculators.financial_plan import FinancialPlanCalculator

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _fake_app(sentiment_label: str):
    def _pipeline(text):
        return [{"label": sentiment_label, "score": 0.99}]

    return SimpleNamespace(state=SimpleNamespace(financial_sentiment_pipeline=_pipeline))


async def test_reads_sentiment_pipeline_from_app_state_not_a_module_global():
    """The whole point of this migration: the calculator must go through
    `self.app.state.financial_sentiment_pipeline`, not any module-level
    pipeline object - swapping the stub on `app.state` must actually change
    the result."""
    app = _fake_app("POSITIVE")
    calc = FinancialPlanCalculator(app)
    result = await calc.calculate_financial_plan(
        principal=1000, rate=5, years=2, market_sentiment="stocks are booming"
    )
    assert result["sentiment"] == "positive"


async def test_positive_sentiment_adjusts_amount_upward():
    app = _fake_app("POSITIVE")
    calc = FinancialPlanCalculator(app)
    result = await calc.calculate_financial_plan(
        principal=1000, rate=5, years=2, market_sentiment="great outlook"
    )
    base = 1000 * (1 + 5 / 100) ** 2
    assert result["investment_value"] == round(base, 2)
    assert result["sentiment_adjusted_value"] == round(base * 1.05, 2)
    assert result["sentiment"] == "positive"


async def test_negative_sentiment_adjusts_amount_downward():
    app = _fake_app("NEGATIVE")
    calc = FinancialPlanCalculator(app)
    result = await calc.calculate_financial_plan(
        principal=1000, rate=5, years=2, market_sentiment="recession fears grow"
    )
    base = 1000 * (1 + 5 / 100) ** 2
    assert result["sentiment_adjusted_value"] == round(base * 0.95, 2)
    assert result["sentiment"] == "negative"


async def test_neutral_sentiment_leaves_amount_unadjusted():
    app = _fake_app("NEUTRAL")
    calc = FinancialPlanCalculator(app)
    result = await calc.calculate_financial_plan(
        principal=1000, rate=5, years=2, market_sentiment="markets are flat today"
    )
    base = 1000 * (1 + 5 / 100) ** 2
    assert result["sentiment_adjusted_value"] == round(base, 2)
    assert result["sentiment"] == "neutral"


@pytest.mark.parametrize(
    "principal,rate,years",
    [(0, 5, 2), (-100, 5, 2), (1000, -1, 2), (1000, 5, 0), (1000, 5, -1)],
)
async def test_invalid_numeric_inputs_raise_value_error(principal, rate, years):
    app = _fake_app("POSITIVE")
    calc = FinancialPlanCalculator(app)
    with pytest.raises(ValueError):
        await calc.calculate_financial_plan(
            principal=principal, rate=rate, years=years, market_sentiment="anything"
        )


async def test_empty_market_sentiment_raises_value_error():
    app = _fake_app("POSITIVE")
    calc = FinancialPlanCalculator(app)
    with pytest.raises(ValueError, match="cannot be empty"):
        await calc.calculate_financial_plan(
            principal=1000, rate=5, years=2, market_sentiment=""
        )


async def test_sentiment_pipeline_failure_propagates():
    """If `app.state.financial_sentiment_pipeline` itself raises (e.g. the
    model failed to load, or some other runtime error), the calculator must
    not swallow it - it re-raises per its own `except Exception: raise`."""

    def _broken_pipeline(text):
        raise RuntimeError("model not initialized")

    app = SimpleNamespace(state=SimpleNamespace(financial_sentiment_pipeline=_broken_pipeline))
    calc = FinancialPlanCalculator(app)
    with pytest.raises(RuntimeError, match="model not initialized"):
        await calc.calculate_financial_plan(
            principal=1000, rate=5, years=2, market_sentiment="anything"
        )
