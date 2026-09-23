"""Content hashing. Lives in `core/` rather than a service because it outlived
the similarity module it used to sit in — `article_key` is now the Article
primary key, so changing how it hashes re-ingests everything still in the feed
window under new ids."""

from __future__ import annotations

import hashlib
import re

_WHITESPACE_RE = re.compile(r"\s+")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_title(title: str) -> str:
    """Collapse the cosmetic differences between two mastheads' renderings of
    the same headline, so they hash together. Not a similarity measure — an
    exact-match normaliser."""
    return _WHITESPACE_RE.sub(" ", title).strip().casefold()


def article_key(canonical_url: str, title: str) -> str:
    """The Article primary key, and therefore the dedupe ledger's identity.
    Deliberately not source-scoped: the same story from a second masthead must
    collide with the first so it can be skipped. See CLAUDE.md."""
    return content_hash(f"{canonical_url}\n{normalize_title(title)}")
