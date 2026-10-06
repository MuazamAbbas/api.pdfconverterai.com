"""Regression coverage for `api.pdfconverterai.com#53` - the DNS-rebinding
TOCTOU affecting every existing `assert_host_is_safe()`-protected endpoint.

`assert_host_is_safe()`/`_sync` resolve a hostname once via `getaddrinfo()`
just to answer "is this safe", but every real caller then made its *own*,
completely independent second connection, which re-resolved the same
hostname again. A malicious short-TTL authoritative DNS server could answer
the first lookup with a safe address and the second, moments later, with a
private one - connecting the caller to an address it never actually
validated.

`resolve_safe()`/`resolve_safe_sync()` (`app/shared/network_security.py`)
and `SafeResolver` (`app/shared/web/pinned_resolver.py`, the `aiohttp`
integration every `web_tools.py`/`seo_audit.py` caller now builds its
session with) close this by collapsing "is it safe" and "what do I connect
to" into a single `getaddrinfo()` call, with the result pinned for the
actual connection - no second, independent resolution ever happens.

These tests prove the rebinding scenario is impossible post-fix: they mock
`socket.getaddrinfo` to return a different (unsafe) answer on any call after
the first, and confirm exactly one resolution happens and the returned
connect target is the first, validated address - never the rebound one.
"""
import socket

import pytest

from app.shared.network_security import UnsafeHostError, resolve_safe, resolve_safe_sync
from app.shared.web.pinned_resolver import SafeResolver

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SAFE_IP = "93.184.216.34"
_UNSAFE_IP = "127.0.0.1"


def _make_rebinding_getaddrinfo(answers: list[str]):
    """Returns `(fake_getaddrinfo, calls)` - `fake_getaddrinfo` mimics
    `socket.getaddrinfo(host, port)`'s real 5-tuple return shape, answering
    with `answers[0]` on the first call and `answers[-1]` (sticking there)
    on every call after - simulating a malicious short-TTL DNS server
    rebinding between a "check" call and a later "connect" call for the
    same hostname. `calls["n"]` counts how many real resolutions happened.
    """
    calls = {"n": 0}

    def _fake(host, port=None, *args, **kwargs):
        index = min(calls["n"], len(answers) - 1)
        calls["n"] += 1
        ip = answers[index]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 0, 0, 0) if family == socket.AF_INET6 else (ip, 0)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    return _fake, calls


# ===========================================================================
# SafeResolver - the aiohttp integration every pinned caller's TCPConnector
# actually resolves/connects through.
# ===========================================================================

async def test_safe_resolver_pins_first_resolution_not_the_rebind_target(monkeypatch):
    """The core rebinding proof: `getaddrinfo` would answer a second call
    with a private address (demonstrated explicitly below), but
    `SafeResolver.resolve()` - what `aiohttp`'s `TCPConnector` actually
    calls to get its connect target - only ever makes ONE such call and
    returns a target pinned to that first, safe address, tagged
    `AI_NUMERICHOST` so nothing downstream performs a second lookup."""
    fake_getaddrinfo, calls = _make_rebinding_getaddrinfo([_SAFE_IP, _UNSAFE_IP])
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    resolver = SafeResolver()
    results = await resolver.resolve("rebinding.example.com", port=443)

    assert calls["n"] == 1, "SafeResolver must resolve exactly once"
    assert len(results) == 1
    assert results[0]["host"] == _SAFE_IP
    assert results[0]["hostname"] == "rebinding.example.com"
    assert results[0]["port"] == 443
    assert results[0]["flags"] & socket.AI_NUMERICHOST, (
        "must be tagged numeric-host so the OS/asyncio layer can't re-resolve"
    )

    # Prove the rebind would actually have happened if anything had made a
    # second real resolution call - the exact race this fix closes.
    second_answer = fake_getaddrinfo("rebinding.example.com", 443)
    assert second_answer[0][4][0] == _UNSAFE_IP
    assert calls["n"] == 2


async def test_safe_resolver_rejects_when_the_single_resolution_is_unsafe(monkeypatch):
    fake_getaddrinfo, calls = _make_rebinding_getaddrinfo([_UNSAFE_IP])
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UnsafeHostError):
        await SafeResolver().resolve("evil.example.com")

    assert calls["n"] == 1


# ===========================================================================
# resolve_safe() / resolve_safe_sync() - the underlying single-resolution
# primitive, exercised directly.
# ===========================================================================

async def test_resolve_safe_resolves_exactly_once_and_returns_the_validated_ip(monkeypatch):
    fake_getaddrinfo, calls = _make_rebinding_getaddrinfo([_SAFE_IP, _UNSAFE_IP])
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    pairs = await resolve_safe("rebinding.example.com")

    assert pairs == [(socket.AF_INET, _SAFE_IP)]
    assert calls["n"] == 1
    # A hypothetical second, independent resolution (what every caller did
    # before this fix) would have landed on the unsafe address - proving
    # the TOCTOU window existed and that resolve_safe()'s single call is
    # what closes it, not a coincidence of the mock.
    assert fake_getaddrinfo("rebinding.example.com")[0][4][0] == _UNSAFE_IP


async def test_resolve_safe_sync_matches_the_async_single_resolution_contract(monkeypatch):
    fake_getaddrinfo, calls = _make_rebinding_getaddrinfo([_SAFE_IP, _UNSAFE_IP])
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    pairs = resolve_safe_sync("rebinding.example.com")

    assert pairs == [(socket.AF_INET, _SAFE_IP)]
    assert calls["n"] == 1


async def test_resolve_safe_raises_unsafe_host_error_for_private_address(monkeypatch):
    fake_getaddrinfo, _calls = _make_rebinding_getaddrinfo([_UNSAFE_IP])
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UnsafeHostError):
        await resolve_safe("evil.example.com")


async def test_resolve_safe_real_ip_literal_resolves_to_itself_with_no_dns():
    """Real (unmocked) exercise - `getaddrinfo()` resolves an IP-literal to
    itself with no network I/O, matching the rest of this test suite's
    convention for exercising the guard logic for real."""
    pairs = await resolve_safe("93.184.216.34")
    assert pairs == [(socket.AF_INET, "93.184.216.34")]


async def test_resolve_safe_real_private_ip_literal_is_rejected():
    with pytest.raises(UnsafeHostError):
        await resolve_safe("127.0.0.1")
