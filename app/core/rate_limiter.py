"""Shared slowapi `Limiter` instance (Handbook Part C.10 - rate limiting).

Extracted out of `app/main.py` (which previously constructed this inline)
so other modules - starting with `app/routers/auth.py`'s brute-force
mitigation on `POST /auth/login` - can import and reuse the exact same
per-IP limiter/keyfunc instead of each route wiring up its own separate
slowapi instance. `app/main.py` still owns registering it on `app.state`
and wiring `RateLimitExceeded`'s exception handler; this module only
constructs the `Limiter` object itself.

Test isolation: this `limiter` is a module-level singleton (imported by
value into `app/main.py` and `app/routers/auth.py` alike), so its in-memory
hit counters persist across tests in the same process unless explicitly
cleared. slowapi's own `Limiter.reset()` does exactly that (clears the
default in-memory `MemoryStorage` backend this Limiter uses - no Redis
involved here, unrelated to the app's ARQ/job-queue Redis). Tests that
exercise rate-limited routes (e.g. `POST /auth/login`'s `10/minute` limit)
should call `limiter.reset()` in a fixture between tests - see
`tests/test_auth.py`'s `_reset_rate_limiter` fixture for the pattern.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
