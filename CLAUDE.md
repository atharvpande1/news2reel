# news2reel

Turns local newspaper feeds into short vertical videos (Instagram Reels /
YouTube Shorts).

Pipeline: `multi-source RSS → cluster & dedupe → rank → editor picks lineup →
per-story ~15s clip → optional merged digest`

**Current phase: 1 — ingestion, clustering and ranking, as a headless FastAPI
backend.** No video, no TTS, no web UI yet. Full spec:
`docs/plan-phase-1.md` — read it before touching pipeline code; this file only
holds what must not be forgotten.

## Stack

Python · FastAPI · SQLAlchemy · SQLite · Alembic · local multilingual
embeddings · scikit-learn TF-IDF. Phase 2 adds a browser-based renderer and
ffmpeg.

## Commands

```bash
uv sync                                  # install
alembic upgrade head                     # apply migrations
uvicorn app.main:app --reload --workers 1
pytest
ruff check . && ruff format .
```

## Layout

```
alembic/             migrations
app/
  main.py            app factory, routers, startup reconciler
  core/config.py     pydantic-settings — thresholds, model names, timeouts, caps
  core/net.py        SSRF-safe fetch (ingest + source check both use it)
  db/models/         Source, Article, Story, Run, ClusterEdge, SeenFingerprint, LlmCall
  schemas/           pydantic request/response models
  api/v1/endpoints/  sources, runs, stories, reports, health
  services/          ingest, similarity, cluster, syndication, score, sensitivity,
                     select, seen, llm, pipeline
  repositories/      deliberately empty in phase 1
```

Business logic lives in `services/`. Endpoints stay thin: validate, call a
service, serialize. Data access sits in services for now — move it to
`repositories/` only when that indirection starts paying.

## Runtime constraints (each is a one-line mistake with an outsized failure)

- **Pipeline entry point is a plain `def`, never `async def`.** Sync tasks run
  in Starlette's threadpool; `async def` doing embedding inference blocks the
  event loop, `/health` stops answering, and the run dies in a restart loop.
- **`--workers 1`.** Each worker loads its own ~1–2GB embedding model, and
  `BackgroundTasks` land in whichever worker served the request, so run state
  would be process-local.
- **SQLite needs `journal_mode=WAL`, `busy_timeout`, `check_same_thread=False`.**
  Without WAL, the single-writer lock produces `database is locked` as soon as
  anything reads during a run.
- **Bind to `127.0.0.1` or require the shared-secret header.** Auth is deferred,
  but `POST /runs` spends real money and pegs a CPU.
- **Schema changes go through Alembic.** Never `create_all()` over an existing
  DB, and never delete the DB to fix a schema problem.

## Invariants (break these and something downstream breaks silently)

- **No files as interfaces.** Runs persist to SQLite, read over HTTP.
  `GET /api/v1/runs/{id}/stories` is the contract phase 2's renderer and phase
  3's editor UI both consume — version it, keep it stable.
- **`Article` is lenient, `Story` is strict.** Article mirrors messy feed
  reality. Story is the render contract: if the renderer needs a field, it's
  required, or the story isn't render-eligible.
- **`SeenFingerprint` and `RunSelectionSnapshot` cannot be rebuilt from feeds** —
  everything else is re-fetchable. This is why migrations are mandatory, why a
  nightly `VACUUM INTO` backup exists, and why deleting the DB is never the fix.
- **Default selection is written once to `RunSelectionSnapshot`**, never a
  mutable column — its diff against the editor's later `selected`/`order` is the
  only labelled data we get for improving the ranker.
- **Sources are archived, never hard-deleted** — Articles hold `source_id`
  forever, so a delete would cascade away history or trip an FK mid-run.
- **`sensitivity_flags` is the union of an LLM pass and a deterministic rule
  pass**, never folded into a score. Article text is untrusted input to the same
  prompt that returns the safety field, so one source means one injection (or
  one ordinary miss) silently removes the editor's signal.
- **The LLM funnel is load-bearing** — prescore cheaply on features, call the
  LLM on the top ~30 clusters only. Never score every article.
- **Per-source failures never abort a run** — record `last_error` on the
  Source, continue, report per-source outcomes in the run response.
- **Run uniqueness is a DB constraint** on `(tenant_id, logical_date, attempt)`.
  A SELECT-then-INSERT check lets two concurrent posts double the LLM spend.
- **A human always approves before publish.** Nothing here publishes autonomously.

## Domain rules

- **Clustering is `0.5 × embedding cosine + 0.5 × TF-IDF cosine`** over a 36h
  window, connected components on the graph. No NER. The lexical half stops two
  unrelated same-category incidents in one city from merging — proper nouns and
  figures are rare, high-IDF tokens, so they separate lexically while staying
  close in embedding space.
- **Components must pass a density check.** Connected components chains under
  single-linkage — A→B at 0.8 and B→C at 0.8 groups A with C even at A→C 0.3 —
  so one bridging article can merge distinct events into a mega-cluster that
  lands at #1. Reject components below the density floor or above
  `MAX_CLUSTER_SIZE` and re-split at a higher threshold.
- **`effective_masthead_count`, never a raw source count.** Six papers running
  the same wire copy is one story from one source, and syndicated copy is the
  most-duplicated text in the corpus — collapse near-identical bodies, count
  distinct `publisher_group`.
- **TF-IDF IDF must be stationary across runs** (`HashingVectorizer` or a
  persisted rolling corpus) — fitting per-run makes a threshold tuned on a
  normal Tuesday misbehave on an election day. Use character n-grams (3–5), not
  words: Indic languages inflect heavily and word-level under-matches variants
  of the same name.
- **Cache embeddings by `content_hash`, store `embedding_model`** — upgrading
  the model shifts the similarity distribution and invalidates the threshold.
- **The 36h window anchors to `Run.logical_date`, not wall-clock**, so a retry
  at a different hour sees the same corpus.
- **Score axes** are `local_relevance, consequence, human_interest, novelty,
  visual_potential`, each with a one-line reason shown to the editor — not
  "virality" or "shock"; every score must be something an editor can argue with.
- **Selection is constrained, not sorted:** one story per cluster, per-category
  caps, videoability floor, fill to 10, keep ~10 alternates.
- **`videoability` is computed, not LLM-scored**, and `has_usable_image` trusts
  feed-declared dimensions rather than fetching (that adds an SSRF surface and
  decompression-bomb exposure). Spoofable, and acceptable: clips are
  typography-first, so the flag never gates rendering.
- **Cross-day dedupe allows follow-ups.** Same fingerprint + new material facts
  → `developing` (eligible); nothing new → `repeat` (suppressed).
- **`category` is a fixed, small enum** (`crime, accident, civic, politics,
  business, education, health, sport, weather, culture, human_interest, other`).
  It drives renderer colour tokens, so adding a value is a renderer change too.
- **Locality is required.** Place-name specificity is the differentiator against
  national news content; a story that can't be localized scores down.

## External input is hostile

Feeds, article text and image URLs are third-party and partly attacker-controlled.

- Fetch only through `core/net.py`: http/https only, private/link-local ranges
  rejected, no redirects into them, response bytes capped.
- Article text enters prompts as delimited data, length-capped, output
  validated against a strict pydantic schema. Render reports with Starlette's
  `Jinja2Templates` (autoescape on, never a bare Jinja `Environment`), and
  scheme-allowlist every URL in `src`/`href`.

## Observability

Structured logs carry `run_id` and `stage`. Per-stage durations on the Run row;
every LLM call in `LlmCall` with tokens and outcome — cost per run is a
first-class number. Cluster edges persisted so a bad cluster is debuggable.

The one alert that matters: **no `ready` run for today by the morning
deadline.** Papers land 5–6am, the desk wants output by 7–8am — a run stuck in
`scoring` at 07:30 is the real failure mode.

## Phase 2 constraints (decided, not yet built)

- **Render text in a browser, composite with ffmpeg** — `ffmpeg drawtext`
  mangles Indic conjuncts and ligatures.
- **~15s per clip means ~38 narrated words** — derive script length from target
  duration, validate against actual TTS audio duration, retry tighter on
  overrun.
- **TTS must return word-level timestamps** — burned-in word-highlighted
  subtitles are mandatory for sound-off viewing; this decides the TTS choice.
- **Clips are typography-first**; photos are an enhancement layer — never drop
  a story for lacking a usable image.
- **The single clip is the primitive**, cached by content hash and
  independently re-renderable. The digest concatenates unmodified clips; no
  digest-specific content leaks into a clip. Safe areas: y=180–1650 on 1080×1920.

## Deliberately out of scope

Do not add without discussion: real auth, Postgres, Redis, Celery/RQ or any task
queue (`BackgroundTasks` + DB checkpointing + startup reconciler), multi-tenancy
and theming, epaper PDF ingestion, publishing APIs, the merged digest, on-screen
source attribution.

## Conventions

- Type-hint everything; pydantic models for all API boundaries.
- Tunables (thresholds, category caps, funnel width, model names, timeouts,
  size caps) go in `core/config.py`, never inline.
- All LLM calls go through `services/llm.py` — retry with backoff, schema
  validation, score caching by `content_hash`, usage logging. A partial scoring
  failure marks `scoring_status: failed`, surfaces as "N of M scored"; it never
  fails the run or silently drops a story.
- Stages checkpoint to the DB so a resume skips completed work.
- Tests target the places where bugs are silent rather than loud:
  `similarity.py`/`cluster.py` (chaining, two-same-city-accidents),
  `syndication.py`, `select.py` (quota logic).
- Verify clustering changes by hand against real feed data — a wrong cluster
  produces a plausible story, not an error.
