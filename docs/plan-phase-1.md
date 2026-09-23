# Phase 1 — Ingestion, Clustering & Ranking (FastAPI backend)

## Context

We are building a system that turns local newspaper feeds into short vertical
videos (Instagram Reels / YouTube Shorts). The full pipeline is:

`multi-source news feeds → cluster & dedupe → rank → editor picks lineup →
per-story 15s clip → optional merged digest`

Phase 1 builds the left half as a **headless FastAPI backend**: feeds in, a
ranked story lineup out over HTTP. No video, no TTS, no web UI. This order was
chosen deliberately so the video assembler (phase 2) develops against real
story data rather than hand-written fixtures.

Settled in design discussion:

- **Output format:** 10 stories × ~15s (~38 narrated words each). The digest is
  a concatenation of unmodified single-story clips, so the single clip is the
  primitive and the digest decision stays deferrable.
- **Clips are typography-first.** Photos are an optional enhancement layer, so
  no story is dropped for lacking an image.
- **A human always approves before publish.** The system proposes a lineup with
  reasons; the editor decides. This is what keeps the required ranking accuracy
  low — the ranker is an assistant, not an authority.
- **Output language matches source language.**
- **Multi-source:** several mastheads. Cross-source corroboration
  (`effective_masthead_count`) is the primary importance signal.
- Deferred: on-screen source attribution, per-tenant source weighting, auth,
  repository layer, multi-tenancy, web UI.

Risk of building this half first is that it defers discovery of clip quality.
Mitigated by the throwaway render spike (near the end).

This revision folds in a production-readiness review. Two findings changed the
algorithm (cluster density, syndication collapse), one reversed an earlier scope
call (**Alembic is in from commit one** — see Architecture), and the rest added
the recovery, security and observability machinery a deadline-bound daily
pipeline needs.

## Success criterion

Run against ~7 days of real feeds and produce a lineup where:

- zero duplicate stories in the top 10,
- no single mega-cluster (see cluster density, step 2),
- the top 10 is not monopolised by one category,
- syndicated wire copy does not dominate purely by republication count,
- a human reviewing the top 10 + alternates agrees with ≥7 of the 10 picks.

If it misses that, suspect **clustering** and **constrained selection** before
touching score weights.

## Architecture

**Stack:** Python, FastAPI, SQLAlchemy, SQLite. Structured as a conventional
FastAPI service so the deferred pieces (auth, repositories) drop into obvious
places later.

**No files as interfaces.** The ranked lineup is an **API response**, not JSON
on disk. Stories persist in SQLite; the phase 2 renderer and the phase 3 editor
UI both consume `GET /api/v1/runs/{id}/stories`. Nothing in the pipeline writes
an artifact another component reads off the filesystem.

**Alembic from commit one.** This reverses the earlier "no migrations in phase
1" decision. `create_all()` creates missing tables but never alters existing
ones, so a schema change silently doesn't apply — and the usual fix is deleting
the SQLite file, which destroys the two datasets that cannot be rebuilt from
feeds: seen-store history and stored default selections. "The schema will
churn", "this data is irreplaceable" and "no migrations" cannot all hold.

**No repository layer in phase 1:** data access lives directly in `services/`.
`repositories/` is where it moves when that indirection starts paying.

**Run execution:** `POST /api/v1/runs` returns `202` with a run id and executes
via FastAPI `BackgroundTasks`; the client polls `GET /api/v1/runs/{id}`.
Deliberately *not* Celery/RQ — single machine, one run per day, no fan-out.
Three constraints make that safe:

- The pipeline entry point is a **plain `def`**, not `async def`, so Starlette
  runs it in the threadpool. An `async def` doing embedding inference blocks the
  event loop, `/health` stops answering, the orchestrator restarts the process,
  and the run dies — a restart loop from one keyword.
- **`--workers 1`.** Each worker would load its own ~1–2GB embedding model, and
  `BackgroundTasks` land in whichever worker served the request, so run state
  would be process-local.
- **Stages checkpoint to the DB** and a startup reconciler resolves runs left
  in a non-terminal state (see Failure recovery).

**SQLite settings**, applied at engine creation: `journal_mode=WAL`,
`busy_timeout=5000`, `check_same_thread=False`. Without WAL the single-writer
lock produces `database is locked` as soon as anything reads during a run.

## Folder structure

```
alembic/                      migrations (versions/ + env.py)
app/
  main.py                     app factory, router registration, startup reconciler
  core/
    config.py                 pydantic-settings — thresholds, model names, timeouts, caps
    logging.py                structured logging
    net.py                    SSRF-safe fetch helper (used by ingest + source check)
  db/
    session.py                engine + SessionLocal, SQLite pragmas
    base.py                   declarative Base
    models/
      source.py               Source
      article.py              Article
      story.py                Story
      run.py                  Run, RunSelectionSnapshot
      cluster.py              ClusterEdge
      seen.py                 SeenFingerprint
      llm.py                  LlmCall
  schemas/                    pydantic request/response models
  api/
    deps.py                   get_db, pagination, shared-secret guard
    v1/
      router.py
      endpoints/              health, sources, runs, stories, reports
  services/
    ingest.py                 fetch (conditional GET), normalise, exact-dedupe
    similarity.py             embeddings + persisted-IDF TF-IDF, combined score
    cluster.py                similarity graph → components → density check
    syndication.py            collapse republished wire copy
    score.py                  prescore funnel, LLM scoring, videoability
    sensitivity.py            deterministic flag rules
    select.py                 constrained selection + alternates
    seen.py                   cross-day fingerprint matching
    llm.py                    provider call wrapper: retry, schema validation, usage logging
    pipeline.py               orchestrates a run, advances Run.stage
  templates/report.html       Jinja run report
  repositories/               deliberately empty in phase 1
tests/
```

Business logic lives in `services/`. Endpoints stay thin: validate, call a
service, serialize.

## Data model

Two levels with deliberately different strictness — this is what resolves the
tension between heterogeneous sources and a fixed render contract:

- **`Article`** — ingestion normalisation target. **Lenient**, most columns
  nullable.
- **`Story`** — a cluster; the render candidate. **Strict**: anything the
  renderer needs is required, or the story is not render-eligible.

### Source

```
id, name, feed_url, language, enabled
publisher_group           groups a masthead's multiple feeds (city edition + main)
category_map              JSON — source section → our category enum
etag, last_modified       for conditional GET
last_success_at, last_error, last_error_at
archived_at               soft delete; never hard-delete (Articles FK here forever)
created_at, updated_at
```

### Article

```
id                    stable hash of (source_id, canonical_url)
source_id             FK
url, canonical_url
title, summary, body_text
published_at, fetched_at
language
feed_position         int — position in feed at fetch time (prominence proxy)
category_raw          nullable, the source's own section label
images                JSON [{url, width, height, credit, caption}]
                      dimensions are feed-declared and NOT verified (see step 5)
embedding             BLOB, cached
embedding_model       model name + version the vector was produced by
content_hash          over title+summary+body; keys the embedding and score caches
raw                   JSON original payload; dropped after RAW_RETENTION_DAYS
```

### Story

```
id, run_id
member_article_ids                  JSON
effective_masthead_count            distinct publisher_groups AFTER syndication collapse
raw_masthead_count                  before collapse; kept for debugging
article_count
cluster_density                     mean intra-cluster similarity
first_seen_at, latest_at
headline, locality, category
scores                              {local_relevance, consequence, human_interest,
                                     novelty, visual_potential, composite}
score_reasons                       JSON [string] — one line per axis
sensitivity_flags                   JSON [enum] — union of LLM + rule pass
sensitivity_source                  which pass raised each flag
videoability                        {has_usable_image, has_number, has_locality,
                                     is_discrete_event, score}
sources                             [{source_name, publisher_group, url, feed_position}]
images                              [{url, w, h, credit, eligible, reject_reason}]
scoring_status                      scored | failed  (see step 4)
status                              new | developing | repeat
selected, order                     editor's overrides; null in phase 1
```

Default selection is **not** a mutable column on Story. It is written once to
`RunSelectionSnapshot` at the end of a run. The diff between that snapshot and
the editor's later `selected`/`order` is the only labelled data we get for
improving the ranker; a mutable column protected by convention would lose it
silently.

Reserve but do not populate: `hook`, `facts[]` (phase 2 outputs). Defining them
now keeps the contract stable across phases.

**Category enum** (fixed and small — drives per-category colour tokens in the
renderer): `crime, accident, civic, politics, business, education, health,
sport, weather, culture, human_interest, other`.

### Run

```
id
tenant_id             defaulted to a single tenant now
logical_date          the news date; UNIQUE (tenant_id, logical_date, attempt)
attempt               increments on force=true
stage                 queued | ingesting | clustering | scoring | selecting | ready | failed
stage_started_at      for the reconciler and the deadline alert
error, error_stage
is_current            one current run per (tenant, logical_date)
started_at, finished_at
```

`UNIQUE (tenant_id, logical_date, attempt)` at the DB level, with
`IntegrityError` caught and the existing run returned. A SELECT-then-INSERT
check would let two concurrent posts both create a run and double the LLM spend.

`tenant_id` is defaulted now purely because the uniqueness constraint is the
expensive thing to change later — everything references it.

### Supporting tables

```
ClusterEdge            run_id, article_a, article_b, embed_sim, tfidf_sim, combined
                       persisted so a bad cluster is debuggable after the fact
LlmCall                run_id, purpose, model, prompt_tokens, completion_tokens,
                       latency_ms, attempt, outcome
SeenFingerprint        keyword set, locality, first_published_at, last_story_id
RunSelectionSnapshot   run_id, ordered story ids, written once, append-only
```

## Pipeline

### 1. Ingest
Poll every enabled, non-archived Source through `core/net.py` (see Security).
Send `If-None-Match` / `If-Modified-Since` from the stored `etag` /
`last_modified`; a `304` skips parsing entirely. Per-source connect and read
timeouts, and a response size cap — the pipeline is deadline-bound, so one hung
feed must not stall it.

Normalise into `Article`, capture `feed_position`. Canonicalise URLs (strip
tracking params) before hashing ids. Exact-dedupe on canonical URL and
normalised title hash.

Per-source failures never abort the run: record `last_error` on the Source row,
continue, report per-source outcomes in the run response.

The candidate window is **36 hours anchored to `Run.logical_date`**, not to
wall-clock. A run retried at a different hour must see the same window.

### 2. Similarity & clustering
No NER. Edge weight is an equal-weighted blend:

```
score = 0.5 * cosine(embedding(title + summary))
      + 0.5 * cosine(tfidf(title + summary))
```

*Why the lexical half matters:* proper nouns and figures are rare, high-IDF
tokens. Two unrelated same-category incidents in one city share generic
vocabulary (`accident`, `dead`, `police`) so they stay close in embedding space,
but differ on place, names and numbers — which is what TF-IDF weights heavily.

Implementation constraints:

- **TF-IDF uses character n-grams (3–5)**, not words. Indic languages inflect
  heavily; word-level matching under-matches variants of the same name.
- **IDF must be stationary across runs.** Fitting the vectorizer on the current
  run's corpus makes IDF weights shift daily, so a threshold tuned on a normal
  Tuesday behaves differently on an election day when half the corpus shares
  vocabulary — clustering quality then drifts for reasons nobody can reproduce.
  Use `HashingVectorizer` with fixed dimensionality, or fit IDF on a persisted
  rolling corpus and version it.
- Embeddings come from a **local multilingual model** (multilingual-e5 / LaBSE
  class). Cache the vector on the Article row keyed by `content_hash`, and store
  `embedding_model` alongside: upgrading the model shifts the similarity
  distribution and invalidates both the cache and the tuned threshold.

Cluster by connected components over the thresholded graph, then **apply a
density check**:

Connected components *is* single-linkage clustering, and single-linkage chains —
A→B at 0.8 and B→C at 0.8 groups A with C even when A→C is 0.3. One generic
bridging article ("crime in the city rose this month") can merge a dozen
distinct events into a mega-cluster, which then reports a large masthead count
and lands at position #1. This is the failure mode that matters most, because it
produces a plausible wrong answer rather than an error.

So: reject any component whose `cluster_density` (mean pairwise combined
similarity) falls below a floor, or whose size exceeds `MAX_CLUSTER_SIZE`, and
re-split it at a higher threshold. Persist every edge to `ClusterEdge`.

### 3. Syndication collapse
Six papers running the same wire copy is one story from one source, not six
editors independently judging it important — and syndicated copy is the
most-duplicated text in the corpus, so uncollapsed it dominates every lineup.

Within a cluster, group members whose body text is near-identical (high lexical
similarity, shared byline or credit) and count each group once. Then compute
`effective_masthead_count` over distinct `publisher_group` values, so one
masthead's city and main editions also count once. Keep `raw_masthead_count`
for debugging.

### 4. Prescore, funnel, score
Cheap feature prescore per cluster — weighted `effective_masthead_count`, best
`feed_position`, recency. Keep the **top ~30**. Everything downstream is
expensive; this funnel is the difference between cents and dollars per run.

One LLM call per surviving cluster via `services/llm.py`, returning per-axis
scores each with a one-line reason:

`local_relevance, consequence, human_interest, novelty, visual_potential`

The same call returns `category` (refining the `category_map` guess) and
`locality` — which is why dropping NER cost nothing: those fields ride along on
a call that happens anyway, for 30 clusters instead of 180 articles.

`services/llm.py` owns:

- **Per-call retry with exponential backoff and jitter.** A 429 mid-scoring is
  the most likely daily failure. Without per-cluster retry the choices are
  failing the whole run (discarding ~16 paid calls) or silently dropping a
  story, which degrades the lineup invisibly.
- **`scoring_status: failed`** on a cluster that exhausts retries, surfaced in
  the report as "27 of 30 scored" so degradation is visible.
- **Score caching by `content_hash`**, so a retried run is near-free rather than
  full price.
- **Usage logging to `LlmCall`.** Margin depends on cost-per-video and nothing
  else measures it; this is also how a prompt change that doubles spend gets
  noticed.
- **Strict output validation** against a pydantic schema — see Security.

### 5. Videoability
Computed, not LLM-scored: `has_locality`, `has_number`, `is_discrete_event`
(from the scoring call), `has_usable_image`.

`has_usable_image` uses **feed-declared dimensions only** (≥1000px on the short
side). Phase 1 does not fetch images to measure them: that would add a second
SSRF surface plus decompression-bomb exposure. The tradeoff is that a publisher
could spoof dimensions — accepted, because clips are typography-first, so the
flag is a small score contribution and never gates rendering.

Analysis and opinion pieces land near zero here: high editorial value,
unwatchable as 15 seconds. Below the floor, a story is excluded from the default
10 but stays available as an alternate.

### 6. Sensitivity flags
The LLM returns flags, and `services/sensitivity.py` computes them independently
from deterministic rules (keyword and pattern matches for minors, sexual
offences, sub judice markers, communal terms). **The stored value is the union**,
with `sensitivity_source` recording which pass raised each.

Article text is untrusted third-party input going into the same prompt that
returns the safety field. A single-sourced flag means one injected instruction —
or one ordinary model miss — silently removes the signal the editor relies on.
Two independent passes mean an injection can suppress at most one.

Flags are never folded into a score. Scores rank; flags inform.

### 7. Select
Constrained selection, **not** a sort: one story per cluster, per-category caps
(e.g. max 3 `crime`, max 2 `accident`), videoability floor, fill to 10 by
composite, keep the next ~10 as alternates. Write the result once to
`RunSelectionSnapshot`.

### 8. Cross-day seen-store
Reuse the **same similarity function** from step 2 against stored fingerprints
with a wider window — one similarity implementation, two windows. A fingerprint
stores the cluster's high-IDF keyword set plus locality.

On a match, compare current content against the stored version: new material
content → `status: developing` (eligible); nothing new → `status: repeat`
(suppressed). A boolean "seen" flag would wrongly kill legitimate follow-ups —
local news covers the same event for days.

## API surface (v1)

```
GET    /health

GET    /api/v1/sources                 list (excludes archived by default)
POST   /api/v1/sources                 create
GET    /api/v1/sources/{id}
PATCH  /api/v1/sources/{id}            includes enabled / archived_at
POST   /api/v1/sources/{id}/archive    soft delete — no hard DELETE endpoint
POST   /api/v1/sources/{id}/check      validate the feed fetches now

POST   /api/v1/runs                    202 + run id; ?force=true → new attempt
GET    /api/v1/runs                    list with stage
GET    /api/v1/runs/{id}               stage, timings, per-source outcomes,
                                       scored/total, token cost

GET    /api/v1/runs/{id}/stories       lineup + alternates as StoryRead
                                       ?include=alternates, ?selected_only=true
GET    /api/v1/runs/{id}/report         HTML run report
```

There is **no hard `DELETE /sources/{id}`**: Articles hold `source_id` forever,
so a delete would either cascade away historical Articles and Stories or trip an
FK mid-run. Archive only.

`force=true` creates a **new attempt** for the same logical date, retains the
previous run, and moves `is_current`. Story reads always require an explicit run
id, so an editor's in-progress selection is never silently re-pointed at a
different lineup.

`GET /api/v1/runs/{id}/stories` is the contract phase 2's renderer and phase 3's
editor UI both consume. It is the most important interface in the project —
version it and keep it stable.

## Security

Auth is still deferred, but the service must not be reachable and
unauthenticated: `POST /runs` spends real money and pegs a CPU, which is a
trivial cost-amplification DoS.

- **Bind to `127.0.0.1`**, or require a shared-secret header via
  `api/deps.py`, until real auth exists. This is a deployment constraint, not
  auth.
- **SSRF guard in `core/net.py`**, used by both ingest and `/sources/{id}/check`
  — these fetch a user-supplied URL: scheme allowlist (`http`/`https` only, so
  no `file://`), resolve DNS and reject loopback, private and link-local ranges
  (cloud metadata at `169.254.169.254` is the obvious target), do not follow
  redirects into those ranges, and cap response bytes so a 500MB "feed" cannot
  OOM the box.
- **Prompt injection.** Article text goes into the scoring prompt as clearly
  delimited data, never as instructions; input length is capped; output is
  validated against a strict pydantic schema and rejected on mismatch. Combined
  with the dual-sourced sensitivity flags (step 6), a publisher who gets
  injected text onto a feed cannot silently promote a story or clear its flags.
- **Report rendering.** Use Starlette's `Jinja2Templates` (autoescape on) and
  never a bare Jinja `Environment`. Autoescape does not cover URLs interpolated
  into `src`/`href`, so scheme-allowlist every rendered URL — `javascript:` in
  an image URL from a spammy feed would otherwise execute in the operator's
  browser, same-origin with the API.
- Secrets from environment via pydantic-settings; never logged.

## Observability

- **Structured logs** with `run_id` and `stage` on every line.
- **Per-stage durations** on the Run row; per-source fetch outcome and latency.
- **`LlmCall` per call** — model, tokens, latency, attempt, outcome. Cost per
  run is a first-class number, not something to reconstruct from a bill.
- **The one alert that matters:** no `ready` run for today's logical date by
  the morning deadline. Papers land 5–6am and the desk wants output by 7–8am, so
  a run stuck in `scoring` at 07:30 is the actual failure mode.
- `Run.error` and `error_stage` populated on failure.
- Cluster edges persisted (step 2) — otherwise debugging a bad cluster after the
  fact is guesswork, and the threshold can never be tuned empirically.

## Failure recovery

- **Stages checkpoint to the DB** — articles after ingest, clusters and edges
  after clustering, scores per cluster as they land. A resume skips completed
  stages, so a failure at cluster 29 does not re-pay for 28 calls.
- **Startup reconciler** in `main.py`: any run in a non-terminal stage whose
  `stage_started_at` exceeds a timeout is resumed or marked `failed` with the
  stage recorded. `BackgroundTasks` share the web process's fate, so without
  this a deploy mid-run leaves a row claiming `scoring` forever.
- **Nightly backup**: `VACUUM INTO` a timestamped copy, shipped off-box.
  Seen-store history and selection snapshots cannot be rebuilt from feeds;
  articles can. That asymmetry is the whole reason to back up.
- **`raw` retention**: dropped after `RAW_RETENTION_DAYS` to bound growth.

Note that "idempotent per logical date" means *memoized*, not reproducible:
scoring is stochastic and feeds mutate. Persisting effective config and LLM
request/response per run is what makes "why was this ranked #1 three weeks ago"
answerable.

## Parallel: throwaway render spike (~1 day, discard the code)

Independent of the backend, worth doing now because two unknowns could
invalidate the phase 2 renderer architecture:

1. Render one ~15s clip of real source-language text (Devanagari or similar)
   through **HTML/browser frame capture → ffmpeg** and confirm script shaping is
   correct. `ffmpeg drawtext` mangles Indic conjuncts and ligatures; a browser
   shapes them properly. Confirm rather than assume.
2. **Measure wall-clock render time** for one clip, extrapolate to 10. The daily
   deadline is roughly 90 minutes end to end including two human review gates;
   render time is the one number that could break it.

Also check the TTS shortlist for **word-level timestamps**. Burned-in
word-highlighted subtitles are mandatory for sound-off viewing, and a TTS
without timestamps forces a forced-alignment detour — so this constraint should
decide the TTS choice, not voice quality.

Throw the spike code away. Its output is three numbers and a yes/no.

## Out of scope for phase 1

Video rendering, TTS, hook/script generation, editor UI, real auth (localhost
bind or shared secret instead), repository layer, multi-tenancy and theming,
epaper PDF ingestion, publishing APIs, the merged digest, on-screen source
attribution, any task queue.

## Verification

**API and lifecycle**

1. `uvicorn app.main:app --workers 1` starts; `/docs` renders; `/health`
   returns ok *while a run is executing* (proves the pipeline is not blocking
   the event loop).
2. Sources CRUD via curl: create two real feeds, list, patch `enabled=false`,
   archive. Confirm no hard-delete endpoint exists.
3. `POST /sources/{id}/check` succeeds on a good feed and returns a structured
   error (not a 500) on a bad URL.
4. `POST /runs` returns 202; `GET /runs/{id}` transitions stages to `ready`.
5. `GET /runs/{id}/stories?selected_only=true` returns exactly 10, all
   validating against `StoryRead` with no null required fields.
6. Two concurrent `POST /runs` for the same date → one run created, the second
   returns the existing one.
7. `?force=true` creates a new attempt, retains the previous run, moves
   `is_current`.

**Clustering — the highest-value checks**

8. Pick 5 stories with `article_count > 1`; confirm every member is genuinely
   the same event.
9. Find two same-category incidents in the same city on one day; confirm they
   did **not** merge.
10. Confirm no cluster exceeds `MAX_CLUSTER_SIZE` and none sits below the
    density floor. Inject a deliberately generic bridging article and confirm it
    does not chain two distinct events.
11. Find a syndicated wire story carried by several papers; confirm
    `effective_masthead_count` is 1 while `raw_masthead_count` is higher.
12. Configure two feeds of one masthead with the same `publisher_group`; confirm
    they count once.
13. Re-run the same date twice with the vectorizer unchanged; confirm identical
    clusters (proves IDF is stationary).

**Ranking and selection**

14. Assert zero repeated events in the top 10 — a duplicate is a clustering bug,
    not a ranking bug.
15. Confirm category caps hold and the top 10 spans categories.
16. Confirm a story with a rule-triggered sensitivity term carries the flag even
    when the LLM did not return it.

**Resilience**

17. Add a deliberately broken feed URL; confirm the run reaches `ready` and
    reports that source's error alongside the successful ones.
18. Point a Source at `http://169.254.169.254/` and at a `file://` path; confirm
    both are rejected before any fetch.
19. Kill the process mid-`scoring`, restart; confirm the reconciler resumes or
    fails the run rather than leaving it stuck.
20. Force an LLM 429; confirm per-cluster retry, then `scoring_status: failed`
    and "N of M scored" in the report rather than a failed run.
21. Re-run after a partial failure; confirm cached scores are reused (check
    `LlmCall` count).
22. Run 7 consecutive days; confirm a followed-up story appears as `developing`
    and an unchanged rerun as `repeat`.
23. Confirm `RunSelectionSnapshot` exists per run and is unchanged by later
    writes.

**Quality**

24. Open `GET /runs/{id}/report`, mark the 10 you would have picked, record
    agreement against the success criterion.
25. Feed a story whose title contains `<script>` and an image URL with a
    `javascript:` scheme; confirm neither executes in the report.
26. Fixture tests for `similarity.py` / `cluster.py` (two-accidents case *and*
    the chaining case), `syndication.py`, and `select.py` (quota logic) — the
    places where bugs are silent rather than loud.
