"""The migrations, run for real.

Every other test builds its schema with `Base.metadata.create_all`, so this is
the only place a migration executes before it reaches the real database. It
runs against its own database: `upgrade head` must build the schema the models
describe (`alembic check` finds no drift), and `downgrade base` must undo it.

Alembic runs in a subprocess because `env.py` takes its URL from
`get_settings()`, which is `lru_cache`d — an in-process run would either reuse
the app's database or need the cache poked. The subprocess also exercises the
command people actually type.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import recreate_database, resolve_test_database_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
async def migrations_url() -> str:
    url = resolve_test_database_url("_migrations")
    await recreate_database(url)
    return url


def alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env={**os.environ, "FEEDCAST_DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


async def test_upgrade_matches_the_models_and_downgrades_cleanly(migrations_url: str) -> None:
    for args in (("upgrade", "head"), ("check",), ("downgrade", "base"), ("upgrade", "head")):
        result = alembic(migrations_url, *args)
        assert result.returncode == 0, f"alembic {' '.join(args)}:\n{result.stderr}"
