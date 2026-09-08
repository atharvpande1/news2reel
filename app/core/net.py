"""SSRF-safe outbound fetch. Every fetch of a third-party URL — feed polling,
`POST /sources/{id}/check` — must go through `fetch_url`, never a raw
`httpx`/`requests` call. See CLAUDE.md: "External input is hostile"."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.core.config import Settings

ALLOWED_SCHEMES = {"http", "https"}


class FetchError(Exception):
    """Base for any failure that stops fetch_url from returning a result."""


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


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"scheme {parsed.scheme!r} is not allowed")
    if not parsed.hostname:
        raise UnsafeUrlError("URL has no hostname")

    try:
        addr_infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"could not resolve host {parsed.hostname!r}") from exc

    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeUrlError(f"{parsed.hostname!r} resolves to non-public address {ip}")


def fetch_url(
    url: str,
    settings: Settings,
    *,
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

    timeout = httpx.Timeout(
        connect=settings.fetch_connect_timeout_seconds,
        read=settings.fetch_read_timeout_seconds,
        write=settings.fetch_read_timeout_seconds,
        pool=settings.fetch_connect_timeout_seconds,
    )

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        while True:
            _validate_public_url(current_url)

            try:
                with client.stream(
                    "GET", current_url, headers=dict(conditional_headers or {})
                ) as response:
                    if response.is_redirect:
                        redirects_followed += 1
                        if redirects_followed > settings.fetch_max_redirects:
                            raise UnsafeUrlError("too many redirects")
                        location = response.headers.get("location")
                        if not location:
                            raise UnsafeUrlError("redirect with no Location header")
                        current_url = str(response.url.join(location))
                        continue

                    content = bytearray()
                    for chunk in response.iter_bytes():
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
