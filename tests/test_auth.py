"""Login, refresh rotation, logout, and the route-table guard.

Uses `anon_client` — a real session with no `current_user` override — because
the point is the layer every other test file overrides away. The failures here
are silent by nature: a router that forgot the dependency serves happily, and a
cookie missing HttpOnly or scoped to the wrong path works in every manual test.
"""

import re
from datetime import timedelta
from http.cookies import SimpleCookie

import pytest
from argon2 import PasswordHasher
from sqlalchemy import delete

from app.api.deps import ACCESS_COOKIE, REFRESH_COOKIE
from app.api.v1.endpoints.auth import REFRESH_PATH
from app.core.config import get_settings
from app.core.security import create_token
from app.db.models.user import User

EMAIL = "editor@example.test"
PASSWORD = "correct horse battery staple"
OPEN_ROUTES = {"/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/logout"}


@pytest.fixture
async def user(db_session) -> User:
    row = User(email=EMAIL, password_hash=PasswordHasher().hash(PASSWORD))
    db_session.add(row)
    await db_session.commit()
    return row


def set_cookies(response) -> dict[str, SimpleCookie]:
    """Set-Cookie headers by cookie name, attributes included — the client's
    own jar drops Path/HttpOnly/SameSite, which are what is under test."""
    parsed = {}
    for header in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        for name, morsel in cookie.items():
            parsed[name] = morsel
    return parsed


async def login(client, email=EMAIL, password=PASSWORD):
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


# --- login ------------------------------------------------------------------


async def test_login_sets_both_cookies_httponly_lax_and_scoped(anon_client, user) -> None:
    response = await login(anon_client)
    assert response.status_code == 200
    assert response.json() == {"id": user.id, "email": EMAIL}

    cookies = set_cookies(response)
    access, refresh = cookies[ACCESS_COOKIE], cookies[REFRESH_COOKIE]
    for morsel in (access, refresh):
        assert morsel["httponly"]
        assert morsel["samesite"].lower() == "lax"
    assert access["path"] == "/"
    # Scoped to the auth routes, so it never rides along on an ordinary call.
    assert refresh["path"] == REFRESH_PATH
    assert int(access["max-age"]) == 15 * 60
    assert int(refresh["max-age"]) == 7 * 24 * 3600
    # Tokens travel only in cookies, never in a body page script could read.
    assert "token" not in response.text


@pytest.mark.parametrize("secure", [True, False])
async def test_the_secure_flag_follows_settings(app, anon_client, user, secure) -> None:
    settings = get_settings().model_copy(update={"cookie_secure": secure})
    app.dependency_overrides[get_settings] = lambda: settings
    cookies = set_cookies(await login(anon_client))
    assert bool(cookies[ACCESS_COOKIE]["secure"]) is secure
    assert bool(cookies[REFRESH_COOKIE]["secure"]) is secure


async def test_wrong_password_and_unknown_email_look_identical(anon_client, user) -> None:
    wrong_password = await login(anon_client, password="nope")
    unknown_email = await login(anon_client, email="nobody@example.test")
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json() == {"detail": "wrong email or password"}
    assert not set_cookies(wrong_password)


async def test_email_is_matched_case_insensitively(anon_client, user) -> None:
    assert (await login(anon_client, email="  Editor@Example.TEST ")).status_code == 200


async def test_a_malformed_hash_fails_closed(anon_client, db_session) -> None:
    """Rows are inserted by hand, so a pasted-wrong hash is a real possibility.
    It must refuse the login, not 500."""
    db_session.add(User(email=EMAIL, password_hash="not-an-argon2-hash"))
    await db_session.commit()
    assert (await login(anon_client)).status_code == 401


# --- access -----------------------------------------------------------------


async def test_a_protected_route_needs_the_access_cookie(anon_client, user) -> None:
    assert (await anon_client.get("/api/v1/cities")).status_code == 401
    await login(anon_client)
    assert (await anon_client.get("/api/v1/cities")).status_code == 200
    assert (await anon_client.get("/api/v1/auth/me")).json()["email"] == EMAIL


async def test_an_expired_access_token_is_refused(anon_client, user) -> None:
    expired = create_token(user.id, "access", timedelta(seconds=-1), get_settings())
    anon_client.cookies.set(ACCESS_COOKIE, expired)
    assert (await anon_client.get("/api/v1/cities")).status_code == 401


async def test_a_refresh_token_is_not_an_access_token(anon_client, user) -> None:
    refresh = create_token(user.id, "refresh", timedelta(minutes=5), get_settings())
    anon_client.cookies.set(ACCESS_COOKIE, refresh)
    assert (await anon_client.get("/api/v1/cities")).status_code == 401


async def test_a_token_signed_with_another_secret_is_refused(anon_client, user) -> None:
    forged = get_settings().model_copy(update={"jwt_secret": "x" * 48})
    anon_client.cookies.set(
        ACCESS_COOKIE, create_token(user.id, "access", timedelta(minutes=5), forged)
    )
    assert (await anon_client.get("/api/v1/cities")).status_code == 401


async def test_deleting_the_user_cuts_off_a_live_session(anon_client, db_session, user) -> None:
    """With no blocklist, this is the per-user kill switch — and it only works
    because every request re-loads the row."""
    await login(anon_client)
    await db_session.execute(delete(User).where(User.id == user.id))
    await db_session.commit()
    db_session.expunge_all()
    assert (await anon_client.get("/api/v1/cities")).status_code == 401
    # The jar still holds the refresh cookie from the login.
    assert (await anon_client.post("/api/v1/auth/refresh")).status_code == 401


# --- refresh ----------------------------------------------------------------


async def test_refresh_rotates_both_tokens(anon_client, user) -> None:
    first = set_cookies(await login(anon_client))
    # Sent by the jar because the path matches — the cookie's scoping at work.
    response = await anon_client.post("/api/v1/auth/refresh")
    assert response.status_code == 200
    second = set_cookies(response)
    assert second[ACCESS_COOKIE].value != first[ACCESS_COOKIE].value
    assert second[REFRESH_COOKIE].value != first[REFRESH_COOKIE].value
    assert second[REFRESH_COOKIE]["path"] == REFRESH_PATH


async def test_refresh_needs_a_refresh_token(anon_client, user) -> None:
    assert (await anon_client.post("/api/v1/auth/refresh")).status_code == 401
    access = create_token(user.id, "access", timedelta(minutes=5), get_settings())
    anon_client.cookies.set(REFRESH_COOKIE, access)
    assert (await anon_client.post("/api/v1/auth/refresh")).status_code == 401


async def test_an_expired_refresh_token_is_refused(anon_client, user) -> None:
    expired = create_token(user.id, "refresh", timedelta(seconds=-1), get_settings())
    anon_client.cookies.set(REFRESH_COOKIE, expired)
    assert (await anon_client.post("/api/v1/auth/refresh")).status_code == 401


# --- logout -----------------------------------------------------------------


async def test_logout_clears_both_cookies(anon_client, user) -> None:
    await login(anon_client)
    response = await anon_client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    cleared = set_cookies(response)
    assert int(cleared[ACCESS_COOKIE]["max-age"]) == 0
    assert int(cleared[REFRESH_COOKIE]["max-age"]) == 0
    # Cleared with the path it was set on, or the browser keeps the original.
    assert cleared[REFRESH_COOKIE]["path"] == REFRESH_PATH
    assert (await anon_client.get("/api/v1/cities")).status_code == 401


# --- the guard ----------------------------------------------------------------


async def test_every_api_route_but_login_refresh_logout_requires_a_session(
    app, anon_client
) -> None:
    """Walks the OpenAPI route listing rather than naming routers, so a router
    added tomorrow without `current_user` fails here instead of shipping open.
    (Not app.routes: FastAPI wraps included routers in a private type that
    hides their paths.) Path parameters are filled with 1 — auth runs before
    any lookup or body validation, so a 401 comes back either way."""
    routes = [
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1")
        for method in operations
    ]
    assert len(routes) > 10  # the walk found the API, or this proves nothing
    for method, path in routes:
        if path in OPEN_ROUTES:
            continue
        url = re.sub(r"\{[^}]+\}", "1", path)
        response = await anon_client.request(method, url)
        assert response.status_code == 401, f"{method} {path} answered {response.status_code}"
