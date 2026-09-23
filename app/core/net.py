"""SSRF-safe outbound fetch. Every fetch of a third-party URL — scheduled feed
polling, `POST /sources/{id}/check` — must go through here, never a raw
`httpx`/`requests` call. See CLAUDE.md: "External input is hostile".

`fetch_url_async` is the real implementation; `fetch_url` only opens a client
for it. One copy of the address policy and of the redirect/size-cap loop —
forking that logic is how an entry point quietly loses a check.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.core.config import Settings

ALLOWED_SCHEMES = {"http", "https"}


class FetchError(Exception):
    """Base for any failure that stops a fetch from returning a result."""


class UnsafeUrlError(FetchError):
    """URL failed validation: bad scheme, unresolvable host, or resolves to a
    private/loopback/link-local/reserved address."""


class FetchTooLargeError(FetchError):
    """Response exceeded settings.fetch_max_response_bytes before finishing."""


class FetchTransportError(FetchError):
    """Network-level failure (timeout, connection refused, TLS error, ...)."""


@dataclass
class FetchResult:
    status_code: int
    headers: httpx.Headers
    content: bytes
    final_url: str


def _reject_non_public(hostname: str, addr_infos) -> None:
    """The address policy. Pure — it takes already-resolved addrinfo tuples."""
    for *_unused, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeUrlError(f"{hostname!r} resolves to non-public address {ip}")


def _parse_target(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"scheme {parsed.scheme!r} is not allowed")
    if not parsed.hostname:
        raise UnsafeUrlError("URL has no hostname")
    return parsed.hostname


async def _validate_public_url(url: str) -> None:
    """`socket.getaddrinfo` is blocking, and this runs on the event loop that
    also carries the scheduler and `/health` — so resolve through the loop's
    executor rather than calling it directly."""
    hostname = _parse_target(url)
    loop = asyncio.get_running_loop()
    try:
        addr_infos = await loop.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"could not resolve host {hostname!r}") from exc
    _reject_non_public(hostname, addr_infos)


def build_timeout(settings: Settings) -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.fetch_connect_timeout_seconds,
        read=settings.fetch_read_timeout_seconds,
        write=settings.fetch_read_timeout_seconds,
        pool=settings.fetch_connect_timeout_seconds,
    )


def build_async_client(settings: Settings) -> httpx.AsyncClient:
    """One client per scheduler tick, so a tick's fetches share a connection
    pool instead of reconnecting per source."""
    return httpx.AsyncClient(timeout=build_timeout(settings), follow_redirects=False)


async def fetch_url_async(
    url: str,
    settings: Settings,
    *,
    client: httpx.AsyncClient,
    conditional_headers: dict[str, str] | None = None,
) -> FetchResult:
    """Fetch a third-party URL defensively.

    Redirects are not followed automatically — each hop is re-validated so an
    open redirect on an allowed host can't be used to reach an internal
    address. Response bytes are capped while streaming, so a large or
    slow-drip body can't exhaust memory.

    Known limitation: validation resolves DNS, then httpx resolves it again to
    connect, leaving a narrow DNS-rebinding TOCTOU window. Closing that fully
    needs a transport that pins the validated IP for the connection — not done
    here. Pair this with network-level egress rules in production; don't rely
    on this check alone.
    """
    current_url = url
    redirects_followed = 0

    while True:
        await _validate_public_url(current_url)

        try:
            async with client.stream(
                "GET", current_url, headers=dict(conditional_headers or {})
            ) as response:
                # has_redirect_location, not is_redirect: the latter is true
                # for *any* 3xx status including 304 Not Modified — a
                # conditional GET hit would otherwise be misread as a redirect
                # with a missing Location header and rejected.
                if response.has_redirect_location:
                    redirects_followed += 1
                    if redirects_followed > settings.fetch_max_redirects:
                        raise UnsafeUrlError("too many redirects")
                    location = response.headers["location"]
                    current_url = str(response.url.join(location))
                    continue

                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > settings.fetch_max_response_bytes:
                        raise FetchTooLargeError(
                            f"response exceeded {settings.fetch_max_response_bytes} bytes"
                        )

                return FetchResult(
                    status_code=response.status_code,
                    headers=response.headers,
                    content=bytes(content),
                    final_url=str(response.url),
                )
        except httpx.HTTPError as exc:
            raise FetchTransportError(str(exc)) from exc


async def fetch_url(
    url: str,
    settings: Settings,
    *,
    conditional_headers: dict[str, str] | None = None,
) -> FetchResult:
    """One-off fetch with its own client — `POST /sources/{id}/check`. The
    scheduler shares one client per tick through `fetch_url_async` instead."""
    async with build_async_client(settings) as client:
        return await fetch_url_async(
            url, settings, client=client, conditional_headers=conditional_headers
        )
