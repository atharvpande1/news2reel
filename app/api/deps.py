from fastapi import Depends, Header, HTTPException, status

from app.core.config import Settings, get_settings
from app.db.session import get_db

__all__ = ["get_db", "require_shared_secret"]


def require_shared_secret(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """No-op when api_shared_secret is unset — acceptable only because the
    runbook binds the dev server to 127.0.0.1. See CLAUDE.md's runtime
    constraints: this guard exists because POST /runs (and, upstream of it,
    outbound fetches) cost real money and CPU if left open."""
    if settings.api_shared_secret is None:
        return
    if x_api_key != settings.api_shared_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key"
        )
