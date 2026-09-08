"""Cross-day dedupe must allow follow-ups, not just suppress repeats — a
boolean "seen" flag would kill legitimate developing stories."""

from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.core.enums import StoryStatus
from app.db.models.seen import SeenFingerprint
from app.services.seen import classify_and_record

NAGPUR = {"name": "Nagpur", "admin_level": "city"}
BUTIBORI_FIRE = "Fire breaks out at a chemical factory in Butibori MIDC, no casualties reported"


def _settings(**overrides) -> Settings:
    defaults = {"seen_window_days": 14}  # seen_similarity_threshold: use the tuned config default
    defaults.update(overrides)
    return Settings(**defaults)


def test_first_occurrence_is_new(db_session) -> None:
    status = classify_and_record(
        db_session,
        story_id=1,
        headline=BUTIBORI_FIRE,
        locality=NAGPUR,
        content_snapshot=BUTIBORI_FIRE,
        settings=_settings(),
        now=datetime.now(UTC),
    )
    assert status == StoryStatus.NEW
    assert db_session.query(SeenFingerprint).count() == 1


def test_unchanged_recurrence_is_repeat(db_session) -> None:
    now = datetime.now(UTC)
    classify_and_record(
        db_session,
        story_id=1,
        headline=BUTIBORI_FIRE,
        locality=NAGPUR,
        content_snapshot=BUTIBORI_FIRE,
        settings=_settings(),
        now=now,
    )
    status = classify_and_record(
        db_session,
        story_id=2,
        headline=BUTIBORI_FIRE,
        locality=NAGPUR,
        content_snapshot=BUTIBORI_FIRE,
        settings=_settings(),
        now=now + timedelta(days=1),
    )
    assert status == StoryStatus.REPEAT
    assert db_session.query(SeenFingerprint).count() == 1  # updated, not duplicated


def test_follow_up_with_new_facts_is_developing(db_session) -> None:
    now = datetime.now(UTC)
    classify_and_record(
        db_session,
        story_id=1,
        headline=BUTIBORI_FIRE,
        locality=NAGPUR,
        content_snapshot=BUTIBORI_FIRE,
        settings=_settings(),
        now=now,
    )
    follow_up = (
        "Butibori MIDC factory fire: pollution control board finds prior safety "
        "violations, factory owner booked under negligence charges, probe ordered"
    )
    status = classify_and_record(
        db_session,
        story_id=2,
        headline="Butibori factory fire: owner booked, probe ordered",
        locality=NAGPUR,
        content_snapshot=follow_up,
        settings=_settings(),
        now=now + timedelta(days=2),
    )
    assert status == StoryStatus.DEVELOPING
    assert db_session.query(SeenFingerprint).count() == 1


def test_different_locality_is_not_matched(db_session) -> None:
    now = datetime.now(UTC)
    classify_and_record(
        db_session,
        story_id=1,
        headline=BUTIBORI_FIRE,
        locality=NAGPUR,
        content_snapshot=BUTIBORI_FIRE,
        settings=_settings(),
        now=now,
    )
    other_city_fire = BUTIBORI_FIRE.replace("Butibori", "Wardha")
    status = classify_and_record(
        db_session,
        story_id=2,
        headline=other_city_fire,
        locality={"name": "Wardha", "admin_level": "city"},
        content_snapshot=other_city_fire,
        settings=_settings(),
        now=now + timedelta(days=1),
    )
    assert status == StoryStatus.NEW
    assert db_session.query(SeenFingerprint).count() == 2


def test_fingerprint_outside_window_is_not_matched(db_session) -> None:
    now = datetime.now(UTC)
    classify_and_record(
        db_session,
        story_id=1,
        headline=BUTIBORI_FIRE,
        locality=NAGPUR,
        content_snapshot=BUTIBORI_FIRE,
        settings=_settings(),
        now=now,
    )
    fp = db_session.query(SeenFingerprint).one()
    fp.updated_at = now - timedelta(days=30)
    db_session.commit()

    status = classify_and_record(
        db_session,
        story_id=2,
        headline=BUTIBORI_FIRE,
        locality=NAGPUR,
        content_snapshot=BUTIBORI_FIRE,
        settings=_settings(seen_window_days=14),
        now=now,
    )
    assert status == StoryStatus.NEW
    assert db_session.query(SeenFingerprint).count() == 2


def test_unrelated_story_same_locality_is_new(db_session) -> None:
    now = datetime.now(UTC)
    classify_and_record(
        db_session,
        story_id=1,
        headline=BUTIBORI_FIRE,
        locality=NAGPUR,
        content_snapshot=BUTIBORI_FIRE,
        settings=_settings(),
        now=now,
    )
    unrelated = "Nagpur civic body approves new drainage project for eastern wards"
    status = classify_and_record(
        db_session,
        story_id=2,
        headline=unrelated,
        locality=NAGPUR,
        content_snapshot=unrelated,
        settings=_settings(),
        now=now + timedelta(days=1),
    )
    assert status == StoryStatus.NEW
    assert db_session.query(SeenFingerprint).count() == 2
