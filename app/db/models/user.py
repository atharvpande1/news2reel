from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base
from app.db.types import UTCDateTime


class User(Base):
    """An editor who can log in. Inserted by hand — there is no signup and no
    user CLI; see CLAUDE.md's "Auth" for the two commands. Deleting a row is
    the per-user kill switch: every request re-loads the user."""

    __tablename__ = "users"
    # Rows are inserted by hand, so a mixed-case email is an easy mistake that
    # would make the account impossible to log into. Refused at insert instead.
    __table_args__ = (CheckConstraint("email = lower(email)", name="users_email_lowercase"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Lowercase, enforced by the check above; login lowercases what was typed,
    # so "Editor@X" and "editor@x" are one account.
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # An Argon2id PHC string ($argon2id$v=19$...).
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.now(), nullable=False
    )
