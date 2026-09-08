"""Orchestrates one run end to end: ingest -> cluster (+ syndication collapse)
-> prescore funnel -> LLM score -> videoability/sensitivity -> select ->
seen-store -> selection snapshot.

Stages checkpoint to the DB (Run.stage advances, and each stage's writes are
committed before moving on) so a resume — via the startup reconciler — skips
completed work rather than re-paying for it. See CLAUDE.md's conventions and
docs/plan-phase-1.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import RunStage, ScoringStatus, StoryStatus
from app.db.models.article import Article
from app.db.models.run import Run, RunSelectionSnapshot
from app.db.models.story import Story
from app.db.session import SessionLocal
from app.services import ingest, sensitivity
from app.services import score as score_service
from app.services import seen as seen_service
from app.services.cluster import cluster_articles
from app.services.llm import LlmClient, ScoringError, build_client, score_cluster
from app.services.select import SelectionCandidate, select_lineup
from app.services.similarity import content_hash as compute_content_hash


def _advance(db: Session, run: Run, stage: str) -> None:
    run.stage = stage
    run.stage_started_at = datetime.now(UTC)
    db.commit()


def _window(run: Run, settings: Settings) -> tuple[datetime, datetime]:
    # Anchored to Run.logical_date, not wall-clock — a retry at a different
    # hour must see the same candidate corpus. See CLAUDE.md's domain rules.
    day_start = datetime.combine(run.logical_date, datetime.min.time(), tzinfo=UTC)
    return day_start - timedelta(hours=settings.cluster_window_hours), day_start + timedelta(days=1)


def _cluster_content_hash(features: score_service.ClusterFeatures) -> str:
    member_hashes = sorted(a.content_hash for a in features.articles)
    return compute_content_hash("|".join(member_hashes))


def _article_text(article: Article) -> str:
    return f"{article.title}\n{article.summary or ''}\n{article.body_text or ''}".strip()


def _score_and_build_story(
    db: Session,
    run: Run,
    features: score_service.ClusterFeatures,
    llm_client: LlmClient,
    settings: Settings,
    now: datetime,
) -> Story | None:
    """One cluster through the LLM, videoability, sensitivity and seen-store.
    Returns None if scoring failed after all retries — the cluster is
    dropped from this run's lineup, never the whole run. See CLAUDE.md."""
    headline = features.articles[0].title
    article_texts = [_article_text(a) for a in features.articles]

    try:
        llm_result = score_cluster(
            db,
            run.id,
            _cluster_content_hash(features),
            headline,
            article_texts,
            llm_client,
            settings,
        )
    except ScoringError:
        return None

    videoability = score_service.compute_videoability(features, llm_result)

    rule_flags = set()
    for text in article_texts:
        rule_flags |= sensitivity.rule_based_flags(text)
    llm_flags = set(llm_result.sensitivity_flags)
    all_flags = rule_flags | llm_flags
    sensitivity_source = {
        flag.value: "both"
        if flag in rule_flags and flag in llm_flags
        else ("rule" if flag in rule_flags else "llm")
        for flag in all_flags
    }

    axis_scores = {
        "local_relevance": llm_result.local_relevance.score,
        "consequence": llm_result.consequence.score,
        "human_interest": llm_result.human_interest.score,
        "novelty": llm_result.novelty.score,
        "visual_potential": llm_result.visual_potential.score,
    }
    composite = sum(axis_scores.values()) / len(axis_scores)
    locality = {"name": llm_result.locality.name, "admin_level": llm_result.locality.admin_level}

    story = Story(
        run_id=run.id,
        member_article_ids=[a.id for a in features.articles],
        effective_masthead_count=features.syndication.effective_masthead_count,
        raw_masthead_count=features.syndication.raw_masthead_count,
        article_count=len(features.articles),
        cluster_density=features.raw_cluster.density,
        first_seen_at=features.first_seen_at,
        latest_at=features.latest_at,
        headline=headline,
        locality=locality,
        category=llm_result.category.value,
        scores={**axis_scores, "composite": composite},
        score_reasons={
            "local_relevance": llm_result.local_relevance.reason,
            "consequence": llm_result.consequence.reason,
            "human_interest": llm_result.human_interest.reason,
            "novelty": llm_result.novelty.reason,
            "visual_potential": llm_result.visual_potential.reason,
        },
        sensitivity_flags=[f.value for f in all_flags],
        sensitivity_source=sensitivity_source,
        videoability=videoability,
        sources=[
            {
                "source_name": a.source.name,
                "publisher_group": a.source.publisher_group,
                "url": a.url,
                "feed_position": a.feed_position,
            }
            for a in features.articles
        ],
        images=[img for a in features.articles for img in a.images],
        scoring_status=ScoringStatus.SCORED,
        status=StoryStatus.NEW,  # placeholder; set for real once the row has an id
    )
    db.add(story)
    db.flush()  # need story.id before recording it on the fingerprint

    story.status = seen_service.classify_and_record(
        db,
        story_id=story.id,
        headline=headline,
        locality=locality,
        content_snapshot="\n".join(article_texts),
        settings=settings,
        now=now,
    )
    return story


def run_pipeline(db: Session, run: Run, settings: Settings, llm_client: LlmClient) -> None:
    now = datetime.now(UTC)

    # 1. Ingest
    _advance(db, run, RunStage.INGESTING)
    outcomes = ingest.ingest_all(db, settings)
    run.source_outcomes = {
        str(o.source_id): {"ok": o.ok, "new_articles": o.new_articles, "error": o.error}
        for o in outcomes
    }
    db.commit()

    # 2/3. Cluster + syndication collapse (collapse happens inside build_features)
    _advance(db, run, RunStage.CLUSTERING)
    window_start, window_end = _window(run, settings)
    articles = list(
        db.scalars(
            sa_select(Article).where(
                Article.published_at >= window_start, Article.published_at < window_end
            )
        )
    )
    raw_clusters = cluster_articles(db, run.id, articles, settings)
    all_features = [score_service.build_features(raw, articles, settings) for raw in raw_clusters]

    # Prescore + funnel — cheap, no LLM call; decides which ~30 clusters are
    # worth the expensive scoring step. See CLAUDE.md: the LLM funnel is
    # load-bearing.
    scored = [(features, score_service.prescore(features, now)) for features in all_features]
    funneled = score_service.apply_funnel(scored, settings)

    # 4-6. LLM score, videoability, sensitivity, seen-store -> Story rows
    _advance(db, run, RunStage.SCORING)
    stories = [
        story
        for features in funneled
        if (story := _score_and_build_story(db, run, features, llm_client, settings, now))
        is not None
    ]
    db.commit()

    # 7. Select
    _advance(db, run, RunStage.SELECTING)
    candidates = [
        SelectionCandidate(
            story_id=s.id,
            category=s.category,
            composite_score=s.scores["composite"],
            videoability_score=s.videoability["score"],
        )
        for s in stories
        if s.status != StoryStatus.REPEAT  # suppressed, never enters the lineup
    ]
    selection = select_lineup(
        candidates,
        lineup_size=settings.lineup_size,
        alternate_size=settings.alternate_size,
        category_cap=settings.category_cap_default,
        videoability_floor=settings.videoability_floor,
    )

    selected_set = set(selection.selected_ids)
    for order, story_id in enumerate(selection.selected_ids):
        story = db.get(Story, story_id)
        story.selected = True
        story.order = order
    for story_id in selection.alternate_ids:
        story = db.get(Story, story_id)
        story.selected = False
    for story in stories:
        if story.id not in selected_set and story.id not in selection.alternate_ids:
            story.selected = False

    db.add(
        RunSelectionSnapshot(
            run_id=run.id,
            selected_story_ids=selection.selected_ids,
            alternate_story_ids=selection.alternate_ids,
        )
    )

    run.finished_at = datetime.now(UTC)
    _advance(db, run, RunStage.READY)


def execute_run_in_background(run_id: int) -> None:
    """The actual `BackgroundTasks` entry point. Opens its own DB session
    rather than reusing the request's — the request returns 202 immediately
    while this can run for minutes, and tying the request-scoped session to
    that duration is the wrong lifetime to depend on."""
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None:
            return
        settings = get_settings()
        stage_at_failure = run.stage
        try:
            client = build_client(settings)
            run_pipeline(db, run, settings, client)
        except Exception as exc:
            db.rollback()
            run = db.get(Run, run_id)
            run.stage = RunStage.FAILED
            run.error = str(exc)
            run.error_stage = stage_at_failure
            db.commit()
    finally:
        db.close()


def reconcile_stuck_runs(db: Session, timeout: timedelta) -> list[int]:
    """Startup reconciler: BackgroundTasks share the web process's fate, so a
    crash or deploy mid-run leaves a Run row claiming e.g. `scoring` forever
    with nothing left to finish it. This does NOT attempt to resume
    mid-pipeline execution — re-running from ingest would create duplicate
    Story rows for clusters a partial attempt already scored, since Story
    rows aren't upserted by cluster identity. It marks the run failed instead;
    recovery is `POST /runs?force=true` for a fresh attempt, which is cheap
    thanks to the LLM score cache (content_hash-keyed, independent of run).
    See CLAUDE.md's observability section."""
    terminal = {RunStage.READY, RunStage.FAILED}
    cutoff = datetime.now(UTC) - timeout
    stuck = list(
        db.scalars(sa_select(Run).where(Run.stage.not_in(terminal), Run.stage_started_at < cutoff))
    )
    for run in stuck:
        run.error = run.error or f"reconciled: stuck in stage {run.stage!r} past timeout"
        run.error_stage = run.stage
        run.stage = RunStage.FAILED
    if stuck:
        db.commit()
    return [run.id for run in stuck]
