"""A DateTime(timezone=True) that never lets a naive value through: assumes UTC
on write if a naive datetime is ever passed, and re-attaches UTC on load, so
Python-side arithmetic against datetime.now(UTC) can never raise TypeError.
Postgres' timestamptz already returns aware values; this guards the edges.
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
