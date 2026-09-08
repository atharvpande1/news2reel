# news2reel

Turns local newspaper feeds into short vertical videos (Instagram Reels /
YouTube Shorts).

Pipeline: `multi-source RSS → cluster & dedupe → rank → editor picks lineup →
per-story ~15s clip → optional merged digest`

**Current phase: 1 — ingestion, clustering and ranking, as a headless FastAPI
backend.** No video, no TTS, no web UI yet. Full spec:
`docs/plan-phase-1.md`.

## Stack

Python · FastAPI · SQLAlchemy · SQLite · Alembic · local multilingual
embeddings · scikit-learn TF-IDF. Phase 2 adds a browser-based renderer and
ffmpeg.

## Commands

The project is being scaffolded; these are the intended entry points.

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

## Runtime constraints

Non-negotiable, and each one is a one-line mistake with an outsized failure:

- **The pipeline entry point is a plain `def`, never `async def`.** Starlette
  runs sync background tasks in the threadpool; an `async def` doing embedding
  inference blocks the event loop, `/health` stops answering, the orchestrator
  restarts the process, and the run dies — a restart loop from one keyword.
- **`--workers 1`.** Each worker loads its own ~1–2GB embedding model, and
  `BackgroundTasks` land in whichever worker served the request, so run state
  would be process-local.
- **SQLite needs `journal_mode=WAL`, `busy_timeout`, `check_same_thread=False`.**
  Without WAL the single-writer lock produces `database is locked` as soon as
  anything reads during a run.
- **Bind to `127.0.0.1` or require the shared-secret header.** Auth is deferred,
  but `POST /runs` spends real money and pegs a CPU.
- **Schema changes go through Alembic.** Never `create_all()` over an existing
  DB, and never delete the DB to fix a schema problem — see the invariant below.

## Invariants

Break these and something downstream breaks silently.

- **No files as interfaces.** Runs persist to SQLite and are read over HTTP.
  Never write an artifact for another component to pick up off disk.
  `GET /api/v1/runs/{id}/stories` is the contract the phase 2 renderer and
  phase 3 editor UI both consume — version it, keep it stable.
- **`Article` is lenient, `Story` is strict.** Article mirrors messy feed
  reality (most columns nullable). Story is the render contract: if the renderer
  needs a field, it is required, or the story is not render-eligible. Do not
  relax Story to accommodate a bad source.
- **Two datasets cannot be rebuilt from feeds:** `SeenFingerprint` history and
  `RunSelectionSnapshot`. Everything else is re-fetchable. This is why
  migrations are mandatory, why the nightly `VACUUM INTO` backup exists, and why
  deleting the DB is never the fix for anything.
- **Default selection is written once to `RunSelectionSnapshot`**, never as a
  mutable column. Its diff against the editor's later `selected`/`order` is the
  only labelled data we get for improving the ranker.
- **Sources are archived, never hard-deleted.** Articles hold `source_id`
  forever; a delete would cascade away history or trip an FK mid-run.
- **`sensitivity_flags` is the union of an LLM pass and a deterministic rule
  pass**, and is never folded into a score. Article text is untrusted input to
  the same prompt that returns the safety field, so a single source means one
  injection — or one ordinary model miss — silently removes the editor's signal.
- **The LLM funnel is load-bearing.** Prescore cheaply on features, then call
  the LLM on the top ~30 clusters only. Never score every article.
- **Per-source failures never abort a run.** Record `last_error` on the Source
  row, continue, report per-source outcomes in the run response.
- **Run uniqueness is a DB constraint** on `(tenant_id, logical_date, attempt)`,
  with `IntegrityError` caught. A SELECT-then-INSERT check lets two concurrent
  posts both create a run and double the LLM spend.
- **A human always approves before publish.** Nothing here publishes
  autonomously. Do not add a path that does.

## Domain rules

- **Clustering is `0.5 × embedding cosine + 0.5 × TF-IDF cosine`**, equally
  weighted, over a 36h window, connected components on the resulting graph.
  There is no NER. The lexical half is what stops two unrelated same-category
  incidents in one city from merging — proper nouns and figures are rare,
  high-IDF tokens, so they separate lexically while staying close in embedding
  space. Do not drop it.
- **Components must pass a density check.** Connected components *is*
  single-linkage clustering, and single-linkage chains: A→B at 0.8 and B→C at
  0.8 groups A with C even when A→C is 0.3. One generic bridging article can
  merge a dozen distinct events into a mega-cluster that then reports a large
  masthead count and lands at #1. Reject components below the density floor or
  above `MAX_CLUSTER_SIZE` and re-split at a higher threshold.
- **`effective_masthead_count`, never a raw source count.** Six papers running
  the same wire copy is one story from one source, and syndicated copy is the
  most-duplicated text in the corpus, so uncollapsed it dominates every lineup.
  Collapse near-identical bodies, then count distinct `publisher_group`.
- **TF-IDF IDF must be stationary across runs.** Fitting per-run makes weights
  shift daily, so a threshold tuned on a normal Tuesday misbehaves on an
  election day. Use `HashingVectorizer` or a persisted rolling corpus.
- **TF-IDF uses character n-grams (3–5), not words.** Indic languages inflect
  heavily; word-level matching under-matches variants of the same name.
- **Cache embeddings by `content_hash` and store `embedding_model`.** Upgrading
  the model shifts the similarity distribution and invalidates both the cache
  and the tuned threshold.
- **The 36h window is anchored to `Run.logical_date`, not wall-clock**, so a
  retry at a different hour sees the same corpus.
- **Score axes** are `local_relevance, consequence, human_interest, novelty,
  visual_potential`, each with a one-line reason shown to the editor. Not
  "virality" or "shock" — every score must be something an editor can argue
  with.
- **Selection is constrained, not sorted:** one story per cluster, per-category
  caps, videoability floor, fill to 10, keep ~10 alternates.
- **`videoability` is computed, not LLM-scored**, and `has_usable_image` trusts
  feed-declared dimensions rather than fetching images (that would add an SSRF
  surface and decompression-bomb exposure). Spoofable, and acceptable: clips are
  typography-first, so the flag never gates rendering.
- **Cross-day dedupe allows follow-ups.** Same fingerprint + new material facts
  → `developing` (eligible); nothing new → `repeat` (suppressed). A boolean
  "seen" flag would kill legitimate follow-ups.
- **`category` is a fixed, small enum** (`crime, accident, civic, politics,
  business, education, health, sport, weather, culture, human_interest, other`).
  It drives per-category colour tokens in the renderer, so adding a value is a
  renderer change too.
- **Locality is required.** Place-name specificity is the entire differentiator
  against national news content. A story that cannot be localized scores down.

## External input is hostile

Feeds, article text and image URLs are third-party and partly attacker-
controlled — local news feeds get spammed and PR-manipulated.

- Fetch only through `core/net.py`: http/https only, private and link-local
  ranges rejected, no redirects into them, response bytes capped.
- Article text enters prompts as clearly delimited data, length-capped, with
  output validated against a strict pydantic schema.
- Render reports with Starlette's `Jinja2Templates` (autoescape on), never a
  bare Jinja `Environment`, and scheme-allowlist every URL put into `src`/`href`
  — autoescape does not stop `javascript:`.

## Observability

Structured logs carry `run_id` and `stage`. Per-stage durations land on the Run
row; every LLM call lands in `LlmCall` with tokens and outcome, because cost per
run is a first-class number and margin depends on it. Cluster edges are
persisted so a bad cluster is debuggable and the threshold is tunable.

The one alert that matters: **no `ready` run for today by the morning
deadline.** Papers land 5–6am, the desk wants output by 7–8am, so a run stuck in
`scoring` at 07:30 is the real failure mode.

## Phase 2 constraints (decided, not yet built)

- **Render text in a browser, composite with ffmpeg.** `ffmpeg drawtext`
  mangles Indic conjuncts and ligatures; a browser shapes them correctly.
- **~15s per clip means ~38 narrated words.** Derive script length from target
  duration, validate against actual TTS audio duration, retry once tighter on
  overrun — never the other way around.
- **TTS must return word-level timestamps.** Burned-in word-highlighted
  subtitles are mandatory for sound-off viewing. This decides the TTS choice,
  not voice quality.
- **Clips are typography-first.** Photos are an enhancement layer. Never drop a
  story for lacking a usable image.
- **The single clip is the primitive.** The digest is a concatenation of
  unmodified clips; no digest-specific content (countdowns, "story 3 of 10")
  may leak into a clip.
- **Every clip is independently re-renderable and cached by content hash**, so
  editing one line of narration re-renders one clip, not the run.
- **Respect platform safe areas:** content between y=180 and y=1650 on 1080×1920.

## Deliberately out of scope

Do not add without discussion: real auth, Postgres, Redis, Celery/RQ or any task
queue (runs use `BackgroundTasks` with DB checkpointing and a startup
reconciler), multi-tenancy and theming, epaper PDF ingestion, publishing APIs,
the merged digest, on-screen source attribution.

## Conventions

- Type-hint everything; pydantic models for all API boundaries.
- Tunables (similarity and density thresholds, category caps, funnel width,
  model names, timeouts, size caps) go in `core/config.py`, never inline.
- All LLM calls go through `services/llm.py` — it owns retry with backoff,
  schema validation, score caching by `content_hash`, and usage logging. A
  partial scoring failure marks `scoring_status: failed` and surfaces as
  "N of M scored"; it never fails the run and never silently drops a story.
- Stages checkpoint to the DB so a resume skips completed work. A failure at
  cluster 29 must not re-pay for 28 calls.
- Tests target the places where bugs are silent rather than loud:
  `similarity.py`/`cluster.py` (two-same-city-accidents *and* chaining),
  `syndication.py`, `select.py` (quota logic).
- Verify clustering changes by hand against real feed data, not just fixtures —
  a wrong cluster produces a plausible story, not an error.
