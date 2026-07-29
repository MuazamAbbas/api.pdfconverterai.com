"""Confirms the `downloaders_youtube` per-function ARQ timeout override
claimed in `app/worker.py::WorkerSettings.functions`
(`func(downloaders_youtube, timeout=600)`) actually takes effect against the
installed `arq==0.28.0`, rather than trusting the comment there. Every other
registered job function should still resolve to the worker's global
`job_timeout = 300` fallback.

`arq.worker.create_worker(settings_cls)` builds a real `arq.worker.Worker`
instance from `WorkerSettings` (`Worker(**get_kwargs(WorkerSettings))`)
without opening a Redis connection - connection setup only happens later,
inside `Worker.async_run()`/`main()` - so this is safe to call synchronously
in a unit test with no real Redis involved.

Per `arq.worker.Worker.__init__`, each entry in `WorkerSettings.functions` is
normalized through `arq.worker.func()` into an `arq.worker.Function`
dataclass with its own `timeout_s` attribute; a plain (non-`func()`-wrapped)
callable gets `timeout_s=None`, and the *effective* timeout used at run time
falls back to the worker's own `job_timeout_s`
(`app/worker.py`'s `job_timeout = 300`) - see `arq/worker.py`'s
`Worker._run_job`: `timeout_s = self.job_timeout_s if function.timeout_s is
None else function.timeout_s`. This test asserts against exactly that
resolution rule.
"""
from arq.worker import create_worker

import app.worker as worker


def test_downloaders_youtube_gets_600s_override_all_others_use_global_300s():
    w = create_worker(worker.WorkerSettings)

    assert w.job_timeout_s == 300, "expected app.worker.WorkerSettings.job_timeout == 300"

    assert "downloaders_youtube" in w.functions
    downloaders_fn = w.functions["downloaders_youtube"]
    assert downloaders_fn.timeout_s == 600, (
        "func(downloaders_youtube, timeout=600) did not produce a 600s "
        "Function.timeout_s override against the installed arq version"
    )

    other_names = [name for name in w.functions if name != "downloaders_youtube"]
    # Sanity check this isn't vacuously true - there must be other job types
    # registered alongside downloaders_youtube.
    assert len(other_names) >= 5

    for name in other_names:
        fn = w.functions[name]
        effective_timeout_s = w.job_timeout_s if fn.timeout_s is None else fn.timeout_s
        assert effective_timeout_s == 300, (
            f"job function {name!r} unexpectedly resolves to a non-default "
            f"timeout ({effective_timeout_s}s) - only downloaders_youtube "
            "should override the global job_timeout"
        )
