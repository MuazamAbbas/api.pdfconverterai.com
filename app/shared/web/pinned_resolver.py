"""`aiohttp` custom resolver that closes the DNS-rebinding TOCTOU window
(`api.pdfconverterai.com#53`, Handbook Part C.10) for every caller that
constructs its `aiohttp.ClientSession` with it.

A `TCPConnector`'s default resolver performs its own, completely independent
`getaddrinfo()` call at actual-connect time - unrelated to any earlier
`assert_host_is_safe()` pre-check a caller already did on the same hostname.
A malicious short-TTL authoritative DNS server can answer the pre-check with
a safe address and the connector's own later resolution, moments after, with
a private one - the race this resolver exists to close.

`SafeResolver.resolve()` is the ONLY resolution `aiohttp` ever performs for a
session configured with it: it calls `app.shared.network_security.resolve_safe`
(a single `getaddrinfo()` + safety check, as one atomic operation - "is this
safe" and "what do I connect to" can no longer be two separate, independently
re-resolvable steps) and hands the validated IP(s) straight back as the
connector's actual connect target, tagged `AI_NUMERICHOST` so the OS/`asyncio`
layer never performs a second, independent lookup of its own.

`UnsafeHostError` is a plain `Exception`, not an `OSError`, so `aiohttp`'s own
`except OSError` wrapping (`connector.py::_create_connection` ->
`ClientConnectorDNSError`) does not swallow it - it propagates out of
`session.get(...)` exactly like a pre-check's `UnsafeHostError` already does,
so no existing `except UnsafeHostError` handler anywhere in `web_tools.py`/
`seo_audit.py` needs to change.

Does NOT help for a literal-IP URL (`http://127.0.0.1/`) - `aiohttp`'s own
`TCPConnector._resolve_host()` recognizes an IP-literal host and never calls
*any* resolver (custom or default) for it at all. That path has no DNS
rebinding risk in the first place (no resolution ever happens for it), and
remains covered by each caller's existing `assert_host_is_safe()` pre-check,
which is unchanged.

Usage: pass `connector=aiohttp.TCPConnector(resolver=SafeResolver())` when
constructing any `aiohttp.ClientSession` that fetches a caller-supplied
hostname. One `SafeResolver` instance per session is sufficient even when
that session's requests span multiple different hostnames (redirect hops, or
the many distinct link hosts `app/services/seo/seo_audit.py` checks) - each
call to `resolve()` validates+resolves fresh, with no pre-registration step
required, so a single instance naturally handles every hostname that session
ever touches.

`app/shared/web/redirect_fetch.py::check_url()` and `app/services/seo/
seo_audit.py::_fetch_main_page()`'s manual redirect loops need no code
change for this - they only ever use whatever `session` they are given/
construct; the pinning lives entirely in how that session's connector is
built by the caller, not in the redirect-following logic itself.
"""
import socket

from aiohttp.abc import AbstractResolver, ResolveResult

from app.shared.network_security import resolve_safe


class SafeResolver(AbstractResolver):
    """SSRF-safe, DNS-rebinding-proof `aiohttp` resolver - see module
    docstring. Stateless (and therefore trivially safe to share or recreate
    per request); kept as its own instance per `TCPConnector` only because
    that is how `aiohttp` itself scopes custom resolvers.
    """

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_UNSPEC
    ) -> list[ResolveResult]:
        # Raises UnsafeHostError (blocked) / socket.gaierror / UnicodeError /
        # asyncio.TimeoutError (genuine resolution failure) - both propagate
        # to the caller unchanged, matching the module docstring's contract.
        pairs = await resolve_safe(host)

        results: list[ResolveResult] = [
            ResolveResult(
                hostname=host,
                host=ip,
                port=port,
                family=fam,
                proto=0,
                flags=socket.AI_NUMERICHOST,
            )
            for fam, ip in pairs
            if family == socket.AF_UNSPEC or fam == family
        ]
        if not results:
            raise socket.gaierror(f"No address of the requested family for host: {host}")
        return results

    async def close(self) -> None:
        """No persistent resources to release - `resolve_safe()` runs each
        lookup as a one-off executor call, not a long-lived client/pool."""
        return None
