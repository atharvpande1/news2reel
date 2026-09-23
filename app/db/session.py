"""Async engine + session factory. `expire_on_commit=False` is load-bearing:
an expired attribute reloads on access, and implicit IO under asyncio raises
`MissingGreenlet` — see CLAUDE.md's runtime constraints."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with SessionLocal() as db:
        yield db
