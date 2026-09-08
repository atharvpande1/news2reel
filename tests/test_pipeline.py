"""End-to-end: real local feeds -> ingest -> cluster -> syndication -> LLM
score (faked) -> videoability/sensitivity -> select -> seen-store -> ready.
Exercises the actual orchestration wiring in pipeline.py, not a mock of it."""

import http.server
import json
from datetime import UTC, date, datetime
from email.utils import format_datetime

from app.core.config import Settings
from app.core.enums import RunStage, StoryStatus
from app.db.models.run import Run, RunSelectionSnapshot
from app.db.models.story import Story
from app.services.pipeline import run_pipeline

LOGICAL_DATE = date(2026, 1, 15)
PUB_DATE = format_datetime(datetime(2026, 1, 15, 10, 0, tzinfo=UTC))


def _rss(items: list[tuple[str, str, str]]) -> str:
    entries = "".join(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<description>{desc}</description><pubDate>{PUB_DATE}</pubDate></item>"
        for title, link, desc in items
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0">'
        f"<channel><title>T</title>{entries}</channel></rss>"
    )


FEED_A = _rss(
    [
        (
            "Fire breaks out at Butibori MIDC chemical factory",
            "https://a.test/butibori-fire",
            "A major fire broke out at a chemical factory in Butibori MIDC on Tuesday night. "
            "Fire brigade officials rushed to the spot as thick smoke rose over the area.",
        )
    ]
)
FEED_B = _rss(
    [
        (
            "Butibori MIDC chemical factory fire brought under control",
            "https://b.test/butibori-fire",
            "The fire at the chemical factory in Butibori MIDC on Tuesday night has been "
            "brought under control, fire brigade officials said, after it broke out and "
            "sent thick smoke over the area.",
        ),
        (
            "City council approves new drainage project",
            "https://b.test/drainage",
            "The civic body approved a new drainage project for the eastern wards on Monday.",
        ),
    ]
)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        body = {"/a.xml": FEED_A, "/b.xml": FEED_B}.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.end_headers()
        self.wfile.write(body.encode())


class ConstantLlmClient:
    """Returns the same valid, schema-conforming response for every call —
    good enough for a wiring test where scoring diversity isn't the point."""

    def __init__(self, category: str = "accident") -> None:
        self.category = category
        self.calls = 0

    def complete(self, *, model: str, system: str, prompt: str):
        from app.services.llm import LlmResponse

        self.calls += 1
        response = {
            "local_relevance": {"score": 7, "reason": "Affects a named ward."},
            "consequence": {"score": 5, "reason": "Contained, no injuries."},
            "human_interest": {"score": 3, "reason": "No named victims."},
            "novelty": {"score": 4, "reason": "Routine coverage."},
            "visual_potential": {"score": 6, "reason": "Smoke and flames."},
            "category": self.category,
            "locality": {"name": "Nagpur", "admin_level": "city"},
            "is_discrete_event": True,
            "sensitivity_flags": [],
        }
        return LlmResponse(text=json.dumps(response), prompt_tokens=100, completion_tokens=50)


def test_pipeline_end_to_end(db_session, make_source, local_http_server, allow_loopback) -> None:
    with local_http_server(_Handler) as base_url:
        make_source(feed_url=f"{base_url}/a.xml", publisher_group="paper-a")
        make_source(feed_url=f"{base_url}/b.xml", publisher_group="paper-b")

        run = Run(logical_date=LOGICAL_DATE)
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)

        settings = Settings()
        client = ConstantLlmClient()
        run_pipeline(db_session, run, settings, client)

    db_session.refresh(run)
    assert run.stage == RunStage.READY
    assert run.finished_at is not None
    assert run.source_outcomes  # per-source outcomes recorded
    assert all(o["ok"] for o in run.source_outcomes.values())

    stories = db_session.query(Story).filter_by(run_id=run.id).all()
    # 3 articles ingested; the two Butibori fire reports should cluster
    # together (near-duplicate, same event) and the drainage story stands
    # alone -> 2 stories, not 3.
    assert len(stories) == 2

    fire_story = next(
        s for s in stories if "fire" in s.headline.lower() or "blaze" in s.headline.lower()
    )
    assert fire_story.effective_masthead_count == 2
    assert fire_story.article_count == 2
    assert fire_story.category == "accident"
    assert fire_story.locality["name"] == "Nagpur"
    assert fire_story.status == StoryStatus.NEW
    assert fire_story.scoring_status == "scored"

    drainage_story = next(s for s in stories if s is not fire_story)
    assert drainage_story.article_count == 1

    # Both stories pass the videoability floor and category cap trivially
    # here — selection wiring, not selection logic (covered in test_select.py).
    selected = [s for s in stories if s.selected is True]
    assert len(selected) == 2
    assert {s.order for s in selected} == {0, 1}

    snapshot = db_session.query(RunSelectionSnapshot).filter_by(run_id=run.id).one()
    assert set(snapshot.selected_story_ids) == {s.id for s in selected}

    # LLM was called once per cluster (2), not once per article (3) — the
    # funnel/cluster collapse actually reduced call volume.
    assert client.calls == 2


def test_pipeline_marks_status_repeat_on_unchanged_rerun(
    db_session, make_source, local_http_server, allow_loopback
) -> None:
    """Cross-day dedupe wired end to end: an identical run the next day must
    suppress the unchanged story as a repeat, not re-select it."""
    with local_http_server(_Handler) as base_url:
        make_source(feed_url=f"{base_url}/a.xml", publisher_group="paper-a")
        make_source(feed_url=f"{base_url}/b.xml", publisher_group="paper-b")

        run1 = Run(logical_date=LOGICAL_DATE)
        db_session.add(run1)
        db_session.commit()
        db_session.refresh(run1)
        run_pipeline(db_session, run1, Settings(), ConstantLlmClient())

        run2 = Run(logical_date=date(2026, 1, 16), attempt=1)
        db_session.add(run2)
        db_session.commit()
        db_session.refresh(run2)
        run_pipeline(db_session, run2, Settings(), ConstantLlmClient())

    stories2 = db_session.query(Story).filter_by(run_id=run2.id).all()
    fire_story2 = next(
        s for s in stories2 if "fire" in s.headline.lower() or "blaze" in s.headline.lower()
    )
    assert fire_story2.status == StoryStatus.REPEAT
    assert fire_story2.selected is False  # suppressed, never enters the lineup
