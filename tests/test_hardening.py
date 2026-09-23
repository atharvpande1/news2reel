"""The go-live hardening: headers, the docs switch, and the input bounds that
used to fall through to a 500 or to nothing at all. Each of these fails silently
in normal use — a missing header is invisible, and an unbounded list works fine
until someone sends a large one."""

import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.main import SECURITY_HEADERS
from app.schemas.carousel import MAX_ENCODED_IMAGE, MAX_SLIDES, ZipRequest

SETTINGS = get_settings()


@pytest.mark.parametrize("path", ["/ui/", "/health", "/api/v1/cities", "/api/v1/auth/me"])
async def test_every_response_carries_the_security_headers(anon_client, path) -> None:
    """UI, API, public and 401 alike — the login page is the one that most
    needs frame-ancestors, and a 401 is still a response a browser renders."""
    response = await anon_client.get(path)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, f"{name} missing on {path}"


async def test_the_csp_allows_no_inline_script_or_foreign_script() -> None:
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    assert "script-src 'self';" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp


async def test_api_docs_are_off_unless_asked_for(monkeypatch, session_factory) -> None:
    import httpx

    import app.main as main

    monkeypatch.setattr(
        main, "get_settings", lambda: SETTINGS.model_copy(update={"api_docs": False})
    )
    app = main.create_app(session_factory=session_factory, start_background=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404, path


async def test_stories_limit_must_be_positive(client) -> None:
    assert (await client.get("/api/v1/stories", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/v1/stories", params={"limit": -5})).status_code == 422


async def test_an_oversized_selection_is_refused_before_the_service(client) -> None:
    ids = [f"id-{n}" for n in range(SETTINGS.carousel_max_stories * 4 + 1)]
    assert (await client.post("/api/v1/carousels", json={"story_ids": ids})).status_code == 422


async def test_saving_a_slide_for_an_unknown_story_is_a_422_not_a_500(
    client, make_source, make_article
) -> None:
    """The deck comes from the browser. An id with no article behind it used to
    reach the foreign key at commit and come back as a 500."""
    article = await make_article(await make_source(), is_city_relevant=True)
    built = (await client.post("/api/v1/carousels", json={"story_ids": [article.id]})).json()
    slides = [{"kind": "story", "story_id": "no-such-article", "text": "x"}]
    response = await client.put(f"/api/v1/carousels/{built['id']}", json={"slides": slides})
    assert response.status_code == 422
    assert "no-such-article" in response.json()["detail"]


async def test_zip_caps_follow_the_settings() -> None:
    """They were hardcoded once, and changing the setting did nothing."""
    assert MAX_SLIDES == SETTINGS.carousel_max_stories + 2
    assert MAX_ENCODED_IMAGE == SETTINGS.zip_max_bytes_per_image * 4 // 3 + 64
    with pytest.raises(ValidationError):
        ZipRequest(images=["x"] * (MAX_SLIDES + 1))
    with pytest.raises(ValidationError):
        ZipRequest(images=["x" * (MAX_ENCODED_IMAGE + 1)])
