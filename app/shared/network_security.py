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
