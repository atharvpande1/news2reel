"""SQLite silently drops tzinfo on DateTime(timezone=True) round-trips — its
sqlite3 adapter has no native timezone-aware TIMESTAMP type, so a value
stored as UTC-aware comes back naive. That's a real, easy-to-hit bug: any
Python-side arithmetic comparing a DB-fetched datetime against a freshly
created datetime.now(UTC) raises TypeError. UTCDateTime re-attaches UTC on
load (and assumes UTC on write if a naive value is ever passed), so every
datetime column behaves consistently. Same DDL as DateTime(timezone=True) —
using this instead of that everywhere needs no migration.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value
