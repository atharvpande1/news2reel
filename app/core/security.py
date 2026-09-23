"""Password verification and JWT issue/decode. Pure — no DB, no HTTP.

Two token kinds, told apart by a `type` claim so a refresh token can never be
presented as an access token or the reverse. See CLAUDE.md's "Auth".
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import Settings

TokenKind = Literal["access", "refresh"]
_ALGORITHM = "HS256"
_hasher = PasswordHasher()

# Verified against when the email is unknown, so a login for a missing account
# costs the same Argon2 work as a wrong password and timing says nothing about
# which emails exist.
DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


class InvalidTokenError(Exception):
    pass


def verify_password(password_hash: str, password: str) -> bool:
    """False on any mismatch — including a malformed hash, which is possible
    because rows are inserted by hand. Fails closed rather than 500ing."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_token(user_id: int, kind: TokenKind, ttl: timedelta, settings: Settings) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "type": kind,
        "iat": now,
        "exp": now + ttl,
        # Unique per token, so a rotation inside the same second still yields
        # new values.
        "jti": secrets.token_urlsafe(12),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_token(token: str, kind: TokenKind, settings: Settings) -> int:
    """The user id, or InvalidTokenError for anything else: bad signature,
    expired, missing claims, or the wrong kind of token."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[_ALGORITHM],
            options={"require": ["exp", "iat", "sub", "type"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
    if claims["type"] != kind:
        raise InvalidTokenError(f"expected a {kind} token")
    try:
        return int(claims["sub"])
    except ValueError as exc:
        raise InvalidTokenError("malformed subject") from exc
