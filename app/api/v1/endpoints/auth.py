"""Login, silent refresh, logout. No signup — users are inserted by hand.

Tokens live only in httpOnly cookies, never in a response body, so page script
cannot read them. See CLAUDE.md's "Auth" for the cookie scoping and what the
absence of a token allowlist does and does not buy.
"""

from datetime import timedelta

from fastapi import APIRouter, Cookie, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ACCESS_COOKIE, REFRESH_COOKIE, current_user, get_db, unauthorized
from app.core.config import Settings, get_settings
from app.core.security import (
    DUMMY_HASH,
    InvalidTokenError,
    create_token,
    decode_token,
    verify_password,
)
from app.db.models.user import User
from app.schemas.auth import LoginRequest, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])

# The refresh cookie is scoped here, so it only ever travels to these routes —
# never alongside an ordinary API call where a log or a proxy might keep it.
REFRESH_PATH = "/api/v1/auth"


def _set_session(response: Response, user: User, settings: Settings) -> None:
    """Issue a fresh pair. Login and refresh both come through here, which is
    what makes /refresh rotate both tokens rather than just the access one."""
    access_ttl = timedelta(minutes=settings.access_token_minutes)
    refresh_ttl = timedelta(days=settings.refresh_token_days)
    common = {"httponly": True, "samesite": "lax", "secure": settings.cookie_secure}
    response.set_cookie(
        ACCESS_COOKIE,
        create_token(user.id, "access", access_ttl, settings),
        max_age=int(access_ttl.total_seconds()),
        path="/",
        **common,
    )
    response.set_cookie(
        REFRESH_COOKIE,
        create_token(user.id, "refresh", refresh_ttl, settings),
        max_age=int(refresh_ttl.total_seconds()),
        path=REFRESH_PATH,
        **common,
    )


@router.post("/login", response_model=UserRead)
async def login(
    data: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """One message for a wrong email and a wrong password, and the same Argon2
    work for both — the dummy hash — so neither the body nor the timing says
    which emails have accounts."""
    user = await db.scalar(select(User).where(User.email == data.email))
    valid = verify_password(user.password_hash if user else DUMMY_HASH, data.password)
    if user is None or not valid:
        raise unauthorized("wrong email or password")
    _set_session(response, user, settings)
    return user


@router.post("/refresh", response_model=UserRead)
async def refresh(
    response: Response,
    feedcast_refresh: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """Rotate both tokens. The old refresh token is not revoked — there is no
    allowlist — so it stays valid until it expires."""
    if feedcast_refresh is None:
        raise unauthorized()
    try:
        user_id = decode_token(feedcast_refresh, "refresh", settings)
    except InvalidTokenError:
        raise unauthorized("session expired") from None
    user = await db.get(User, user_id)
    if user is None:
        raise unauthorized()
    _set_session(response, user, settings)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, settings: Settings = Depends(get_settings)) -> None:
    """Clears this browser's cookies and nothing else — a copied token still
    works until it expires. Open to anyone: logging out needs no session."""
    common = {"httponly": True, "samesite": "lax", "secure": settings.cookie_secure}
    response.delete_cookie(ACCESS_COOKIE, path="/", **common)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH, **common)


@router.get("/me", response_model=UserRead)
async def me(user: User = Depends(current_user)) -> User:
    return user
