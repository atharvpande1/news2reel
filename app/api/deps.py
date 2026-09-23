from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import InvalidTokenError, decode_token
from app.db.models.user import User
from app.db.session import get_db

__all__ = ["ACCESS_COOKIE", "REFRESH_COOKIE", "current_user", "get_db", "unauthorized"]

ACCESS_COOKIE = "feedcast_access"
REFRESH_COOKIE = "feedcast_refresh"


def unauthorized(detail: str = "not logged in") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


async def current_user(
    feedcast_access: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """The logged-in editor, from the access cookie. Re-loads the row on every
    request: with no token blocklist, deleting the user is the only way to cut
    off a token that is still inside its lifetime. See CLAUDE.md's "Auth"."""
    if feedcast_access is None:
        raise unauthorized()
    try:
        user_id = decode_token(feedcast_access, "access", settings)
    except InvalidTokenError:
        raise unauthorized("session expired") from None
    user = await db.get(User, user_id)
    if user is None:
        raise unauthorized()
    return user
