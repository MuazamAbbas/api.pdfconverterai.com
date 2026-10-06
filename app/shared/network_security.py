"""SSRF hardening helpers (Handbook Part C.10, Secure by Default).

Centralizes the "is it safe to connect to this user-supplied host" check so
every router that resolves/connects to an arbitrary caller-supplied hostname
(`web_tools.py`'s `validate_url`/`website_down_detector`/`dns_lookup`/
`ssl_checker` today) shares one implementation instead of drifting into
divergent copies. There was previously no SSRF protection anywhere in this
backend (confirmed by a full-repo grep) - the YouTube downloader SSRF gap is
tracked separately as `api.pdfconverterai.com#52` and is out of scope here,
but new code must not repeat the same mistake.

`assert_host_is_safe()` resolves the hostname itself (this also covers
IP-literal input like `127.0.0.1` or `169.254.169.254` - `getaddrinfo()`
resolves a literal IP to itself, so it hits the exact same private/loopback/
link-local/reserved/multicast check as a DNS-mediated SSRF attempt) and
raises `UnsafeHostError` if ANY resolved address is not safe to connect to.
It deliberately does NOT raise on a genuine resolution failure (NXDOMAIN,
timeout, etc.) - that is not an SSRF verdict, and is left to the caller's
own DNS/connect logic to surface as a normal "couldn't resolve/connect"
result.

`resolve_safe()`/`resolve_safe_sync()` (added for `api.pdfconverterai.com#53`,
the DNS-rebinding TOCTOU) close a gap `assert_host_is_safe()` itself cannot:
every caller resolved a hostname once just to get a yes/no verdict, then
made its own, completely separate real connection - which re-resolved the
same hostname a second time, independently. A malicious short-TTL DNS
server can answer those two lookups differently. `resolve_safe()` collapses
"is it safe" and "what do I connect to" into one `getaddrinfo()` call, and
returns the validated address(es) for the caller to connect to directly -
see `app/shared/web/pinned_resolver.py::SafeResolver` (the `aiohttp`
integration) and `app/routers/web_tools.py`'s `_fetch_certificate_der`/
`_SafeWhoisSocket` (the raw-`socket` ones) for how each caller uses it.
"""
import asyncio
import ipaddress
import socket


class UnsafeHostError(Exception):
    """Raised when a hostname resolves to (or literally is) a private,
    loopback, link-local, reserved, or multicast address. Callers should
    catch this one type and map it to a clean, client-safe error message -
    never let a raw socket/DNS exception reach the client."""


def _is_unsafe_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # Not a parseable IP at all - treat as unsafe rather than silently
        # letting an unrecognized address shape through.
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def assert_host_is_safe(hostname: str, timeout: float = 5.0) -> None:
    """Raises `UnsafeHostError` if `hostname` resolves to any private/
    loopback/link-local/reserved/multicast address. No-op (returns
    normally) on resolution failure - that's the caller's own lookup logic
    to report, not an SSRF verdict.
    """
    if not hostname:
        return

    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.run_in_executor(None, socket.getaddrinfo, hostname, None),
            timeout=timeout,
        )
    except (socket.gaierror, UnicodeError, asyncio.TimeoutError):
        return

    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0] if sockaddr else None
        if ip_str and _is_unsafe_ip(ip_str):
            raise UnsafeHostError(f"Host resolves to a disallowed network address: {hostname}")


def assert_host_is_safe_sync(hostname: str) -> None:
    """Synchronous twin of `assert_host_is_safe()`, for guarding a connect
    that happens deep inside a third-party library running on a blocking
    worker thread (`asyncio.to_thread()`) with no running event loop to
    `await` against - e.g. `web_tools.py`'s WHOIS referral-host guard,
    which has to intercept a raw `socket.connect()` call made from inside
    `python-whois`'s own code. Same private/loopback/link-local/reserved/
    multicast check as the async version, via a direct (blocking)
    `socket.getaddrinfo()` call instead of routing through an executor.
    Same "no-op on resolution failure, that's not an SSRF verdict" contract
    as the async version too.
    """
    if not hostname:
        return

    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        return

    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0] if sockaddr else None
        if ip_str and _is_unsafe_ip(ip_str):
            raise UnsafeHostError(f"Host resolves to a disallowed network address: {hostname}")


def _dedupe_resolved(infos) -> list[tuple[int, str]]:
    """Shared by `resolve_safe`/`resolve_safe_sync`: walks a raw
    `getaddrinfo()` result list, validates every resolved address (raising
    `UnsafeHostError` on the first unsafe one - same all-or-nothing contract
    `assert_host_is_safe`/`_sync` already use), and returns the deduplicated
    `(family, ip)` pairs in `getaddrinfo()`'s own returned order."""
    results: list[tuple[int, str]] = []
    seen: set[str] = set()
    for info in infos:
        family = info[0]
        sockaddr = info[4]
        ip_str = sockaddr[0] if sockaddr else None
        if not ip_str:
            continue
        if _is_unsafe_ip(ip_str):
            raise UnsafeHostError("Host resolves to a disallowed network address")
        if ip_str not in seen:
            seen.add(ip_str)
            results.append((family, ip_str))
    return results


async def resolve_safe(hostname: str, timeout: float = 5.0) -> list[tuple[int, str]]:
    """Resolves `hostname` to its IP address(es) via a single `getaddrinfo()`
    call, validating every result as part of that same operation - closing
    the DNS-rebinding TOCTOU `api.pdfconverterai.com#53` tracks: every
    existing caller resolved once via `assert_host_is_safe()` for a yes/no
    verdict, then made its *own*, completely independent real connection,
    which re-resolved the hostname a second time. A malicious short-TTL
    authoritative DNS server could answer the first lookup with a safe
    address and the second, moments later, with a private one.

    Returns the resolved `(family, ip)` pairs for the caller to connect to
    directly instead of re-resolving `hostname` itself - see
    `app/shared/web/pinned_resolver.py::SafeResolver` for the `aiohttp`
    integration, and `app/routers/web_tools.py`'s `_fetch_certificate_der`/
    `_SafeWhoisSocket` for the raw-socket ones. Raises `UnsafeHostError` if
    ANY resolved address is unsafe (same all-or-nothing contract as
    `assert_host_is_safe`).

    Unlike `assert_host_is_safe`, a genuine resolution failure is NOT
    swallowed as a no-op here: `socket.gaierror`/`UnicodeError` propagate,
    and so does `asyncio.TimeoutError` - a caller that needs a real address
    to actually connect to has no safe "proceed anyway" default the way a
    pure yes/no safety verdict does. Existing callers already handle these
    exception shapes arriving from their own DNS/connect layer (e.g.
    `aiohttp.ClientConnectorDNSError` wraps a `socket.gaierror`-shaped
    failure the same way `TCPConnector` itself would raise it), so this is a
    propagation-point change, not a new failure mode they have to learn.
    """
    if not hostname:
        raise socket.gaierror("Empty hostname cannot be resolved")

    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.run_in_executor(None, socket.getaddrinfo, hostname, None),
        timeout=timeout,
    )

    results = _dedupe_resolved(infos)
    if not results:
        raise socket.gaierror(f"No usable address found for host: {hostname}")
    return results


def resolve_safe_sync(hostname: str) -> list[tuple[int, str]]:
    """Synchronous twin of `resolve_safe()`, for the raw-`socket`/blocking
    call sites (`app/routers/web_tools.py`'s `_fetch_certificate_der`/
    `_SafeWhoisSocket.connect()`) that resolve-then-connect inline, with no
    running event loop to `await` against. Same single-`getaddrinfo()`,
    resolve-and-validate-as-one-operation contract as the async version,
    including propagating (not swallowing) a genuine resolution failure.
    """
    if not hostname:
        raise socket.gaierror("Empty hostname cannot be resolved")

    infos = socket.getaddrinfo(hostname, None)
    results = _dedupe_resolved(infos)
    if not results:
        raise socket.gaierror(f"No usable address found for host: {hostname}")
    return results
