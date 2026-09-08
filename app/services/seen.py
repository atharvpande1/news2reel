"""Cross-day dedupe. Same fingerprint + new material facts -> developing
(eligible); nothing new -> repeat (suppressed). A boolean "seen" flag would be
wrong: local news follows the same event for days, and follow-ups are
legitimate stories.

Reuses similarity.py's combined embedding+TF-IDF score — the same
implementation as clustering, just a wider window — rather than a second
bespoke matcher. This matters more here than it sounds: a legitimate
follow-up often rewords the headline entirely, and pure lexical (TF-IDF)
overlap alone is too brittle to catch that the body is still about the same
event. The embedding half is what recognizes it as semantically the same
story. See CLAUDE.md's domain rules and docs/plan-phase-1.md step 8.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import StoryStatus
from app.db.models.seen import SeenFingerprint
from app.services.similarity import embed_texts, pairwise_similarity, tfidf_matrix

_TOKEN_RE = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def extract_keywords(text: str, top_k: int = 20) -> list[str]:
    """Unique alphabetic tokens of length >=4, in order of first appearance.
    Stored on the fingerprint as a compact, human-inspectable summary for
    debugging — matching itself compares against the full content_snapshot,
    not this list (a natural-sentence-vs-keyword-bag comparison measurably
    understates embedding similarity)."""
    seen: list[str] = []
    for token in _TOKEN_RE.findall(text.lower()):
        if token not in seen:
            seen.append(token)
        if len(seen) >= top_k:
            break
    return seen


def _combined_similarity_one_to_many(
    query_text: str, candidate_texts: list[str], settings: Settings
) -> np.ndarray:
    texts = [query_text, *candidate_texts]
    embeddings = embed_texts(texts, settings)
    tfidf = tfidf_matrix(texts, settings)
    _embed_sim, _tfidf_sim, combined = pairwise_similarity(embeddings, tfidf)
    return combined[0, 1:]


def find_matching_fingerprint(
    db: Session, query_text: str, locality: dict, settings: Settings, now: datetime
) -> SeenFingerprint | None:
    """Best match within the seen window, restricted to the same locality
    name (similarity alone would happily match "school fire" in two
    different cities) and clearing seen_similarity_threshold.

    `query_text` should combine headline + content, same as how the stored
    keywords were extracted."""
    window_start = now - timedelta(days=settings.seen_window_days)
    candidates = list(
        db.scalars(select(SeenFingerprint).where(SeenFingerprint.updated_at >= window_start))
    )
    same_locality = [c for c in candidates if c.locality.get("name") == locality.get("name")]
    if not same_locality:
        return None

    candidate_texts = [c.content_snapshot for c in same_locality]
    sims = _combined_similarity_one_to_many(query_text, candidate_texts, settings)

    best_idx = int(sims.argmax())
    if sims[best_idx] >= settings.seen_similarity_threshold:
        return same_locality[best_idx]
    return None


def classify_and_record(
    db: Session,
    *,
    story_id: int,
    headline: str,
    locality: dict,
    content_snapshot: str,
    settings: Settings,
    now: datetime,
) -> StoryStatus:
    """Match against fingerprint history and update it. Call once per
    surviving cluster during selection, after the LLM call has supplied
    locality (this needs a real locality to restrict matching to)."""
    query_text = f"{headline} {content_snapshot}"
    match = find_matching_fingerprint(db, query_text, locality, settings, now)

    if match is None:
        db.add(
            SeenFingerprint(
                keywords=extract_keywords(query_text),
                locality=locality,
                # content_snapshot always stores headline+content combined —
                # matching (above) and the repeat-vs-developing comparison
                # (below) both need to compare like-for-like against this.
                content_snapshot=query_text,
                first_published_at=now,
                last_story_id=story_id,
            )
        )
        db.flush()
        return StoryStatus.NEW

    # Compare the new content against what was last published for this
    # fingerprint: near-identical -> nothing new -> repeat; otherwise the
    # follow-up carries new material facts -> developing.
    similarity_to_previous = _combined_similarity_one_to_many(
        query_text, [match.content_snapshot], settings
    )[0]

    match.content_snapshot = query_text
    match.last_story_id = story_id
    db.flush()

    if similarity_to_previous >= settings.syndication_similarity_threshold:
        return StoryStatus.REPEAT
    return StoryStatus.DEVELOPING
