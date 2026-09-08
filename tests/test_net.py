"""core/net.py is the one thing every outbound fetch must go through — see
CLAUDE.md's "External input is hostile". Validation is tested hermetically via
monkeypatched DNS; fetch mechanics (byte cap, redirects) against a local
server, with validation stubbed out since 127.0.0.1 is itself loopback."""

import http.server
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.core.net import (
    FetchTooLargeError,
    FetchTransportError,
    UnsafeUrlError,
    _validate_public_url,
    fetch_url,
)


def _addrinfo_for(ip: str) -> list[tuple]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.mark.parametrize(
    "ip",
    ["10.0.0.5", "172.16.0.1", "192.168.1.1", "127.0.0.1", "169.254.169.254", "0.0.0.0"],
)
def test_validate_public_url_rejects_non_public_ip(monkeypatch, ip: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: _addrinfo_for(ip))
    with pytest.raises(UnsafeUrlError, match="non-public address"):
        _validate_public_url("http://example.test/feed")


def test_validate_public_url_accepts_public_ip(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: _addrinfo_for("93.184.216.34"))
    _validate_public_url("http://example.test/feed")  # does not raise


def test_validate_public_url_rejects_file_scheme() -> None:
    with pytest.raises(UnsafeUrlError, match="scheme"):
        _validate_public_url("file:///etc/passwd")


def test_validate_public_url_rejects_unresolvable_host(monkeypatch) -> None:
    def _raise(*_a, **_kw):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    with pytest.raises(UnsafeUrlError, match="could not resolve"):
        _validate_public_url("http://nonexistent.invalid/feed")


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # silence test output
        pass

    def do_GET(self) -> None:
        if self.path == "/redirect-once":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", "/redirect-loop")
            self.end_headers()
        elif self.path == "/big":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * 1000)
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")


@contextmanager
def _local_server() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture
def settings() -> Settings:
    return Settings(fetch_max_response_bytes=5_000_000, fetch_max_redirects=3)


@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch):
    """These tests target 127.0.0.1 on purpose (a local test server) — bypass
    the loopback rejection that test_validate_public_url_* above verifies on
    its own, so these tests exercise fetch_url's mechanics instead."""
    import app.core.net as net

    monkeypatch.setattr(net, "_validate_public_url", lambda _url: None)


def test_fetch_url_returns_content_and_status(settings: Settings) -> None:
    with _local_server() as base_url:
        result = fetch_url(f"{base_url}/ok", settings)
    assert result.status_code == 200
    assert result.content == b"ok"


def test_fetch_url_follows_redirect_within_limit(settings: Settings) -> None:
    with _local_server() as base_url:
        result = fetch_url(f"{base_url}/redirect-once", settings)
    assert result.status_code == 200
    assert result.final_url.endswith("/ok")


def test_fetch_url_raises_on_redirect_loop(settings: Settings) -> None:
    settings.fetch_max_redirects = 2
    with _local_server() as base_url, pytest.raises(UnsafeUrlError, match="too many redirects"):
        fetch_url(f"{base_url}/redirect-loop", settings)


def test_fetch_url_raises_on_response_too_large(settings: Settings) -> None:
    settings.fetch_max_response_bytes = 10
    with _local_server() as base_url, pytest.raises(FetchTooLargeError):
        fetch_url(f"{base_url}/big", settings)


def test_fetch_url_wraps_connection_failure(settings: Settings) -> None:
    with pytest.raises(FetchTransportError):
        fetch_url("http://127.0.0.1:1/rss", settings)  # nothing listens on port 1
