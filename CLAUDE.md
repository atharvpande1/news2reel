# Feedcast

Turns local newspaper feeds into publishable Instagram carousels, and later
short vertical videos (Reels / Shorts).

Pipeline: `multi-source RSS (scheduled per-source polling) → hash dedupe →
relevance filter → engagement rank → GET /stories → editor picks ≤8 →
Creative Canvas → zip of JPEGs → Gallery`

**Current phase: 1 — scheduled ingestion, LLM relevance / category / impact
classification, engagement ranking, and a browser UI where an editor picks
stories, edits the slides on a canvas, downloads the carousel and finds it again
in the Gallery.**
No video, no TTS, no publishing — a human still uploads. `docs/plan-phase-1.md`
describes the removed clustering/run design and `docs/carousel-workflow.md` the
canvas; this file wins where they disagree.

## Stack

Python · FastAPI · SQLAlchemy (async) · Postgres via asyncpg · Alembic · httpx ·
feedparser · Docker Compose (two containers: `api`, `db`).
Phase 2 adds a browser-based renderer and ffmpeg.

## Commands

```bash
cp .env.local.example .env.local         # once; fill in the LLM key
docker compose up --build                # local: db → alembic upgrade head → uvicorn --reload
docker compose exec api pytest           # against feedcast_test, never the dev DB
docker compose exec api ruff check . && docker compose exec api ruff format .
docker compose exec api alembic revision --autogenerate -m "..."
docker compose up -d --build            # prod — .env there sets COMPOSE_FILE, see README
```

`docker-compose.yml` is the base; `docker-compose.override.yml` (local, loaded
automatically) adds `.env.local`, the bind mount, `--reload` and dev deps;
`docker-compose.prod.yml` adds `.env.prod`. One `Dockerfile` serves both. **On
the server a gitignored `.env` sets
`COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml`** — without it a bare
`docker compose up` auto-loads the local override (bind mount, `--reload`,
`.env.local`) in production. Prod is AlmaLinux + Docker CE with nginx on the
host terminating TLS (`deploy/nginx.conf`); the deploy runbook is in README.md.

## Layout

```
alembic/             migrations
app/
  main.py            app factory, routers, scheduler loop, classifier loop
  core/config.py     pydantic-settings — intervals, weights, concurrency, caps
  core/net.py        SSRF-safe fetch (async)
  db/models/         City, Source, Article, LlmCall, Selection, Carousel,
                     CarouselSlide
  schemas/           pydantic request/response models
  api/v1/endpoints/  cities, sources, stories, carousels, health
  services/          ingest, scheduler, classify, rank, stories, carousel,
                     gallery, cities, sources, sensitivity, llm
  static/            the UI — vanilla ES modules, no build step, mounted /ui
  repositories/      deliberately empty in phase 1
```

Business logic lives in `services/`; endpoints stay thin (validate, call a
service, serialize). Data access sits in services for now — move it to
`repositories/` only when that indirection starts paying.

## Runtime constraints (each is a one-line mistake with an outsized failure)

- **Everything touching the DB is `async` on the event loop.** Endpoints,
  services and both background loops use `AsyncSession`; the loops open one per
  unit of work with `async with session_factory()`. The only `to_thread` calls
  left are the two sync things that would otherwise stall `/health` and every
  in-flight fetch: `feedparser.parse` and the OpenAI SDK call.
- **Relationships are `lazy="raise_on_sql"`.** An implicit lazy load under
  asyncio fails with `MissingGreenlet`, so every relationship a caller reads is
  eager-loaded (`selectinload`/`joinedload`) at the query that fetched it; an
  identity-map hit (a source's already-loaded city) needs no SQL and is allowed.
  Sessions are `expire_on_commit=False` for the same reason — refresh
  explicitly when a server-side value is needed after a commit.
- **`--workers 1`.** Each worker starts its own scheduler and classifier loops
  and would poll every source, and re-classify every article, N times over.
- **Publish on `127.0.0.1`.** Both compose files map the port to
  `127.0.0.1:8000` only; the world reaches it through nginx on the host, which
  owns TLS, HSTS, the request-size cap and the login rate limit. Secure cookies
  mean the app is unusable over plain http, so TLS is not optional.
- **The live Postgres role, database and volume are still named `news2reel`.**
  The rename to Feedcast stopped at the database on purpose: renaming a role
  resets its password and the volume name is what holds the data. Everything
  else — env prefix `FEEDCAST_`, the UI, storage keys — is Feedcast.
- **Schema changes go through Alembic.** Never `create_all()` over an existing
  DB, and never drop the `pgdata` volume to fix a schema problem. The container
  runs `alembic upgrade head` on start. History was squashed to one Postgres
  baseline on 2026-09-23; the SQLite era lives only in the `*.db` backups.

## Auth

- **Every `/api/v1` router depends on `current_user`**, except `/auth/login`,
  `/auth/refresh` and `/auth/logout`. `/health` and `/ui` (static, no data) are
  public. `tests/test_auth.py` walks the route table, so a router that forgets
  the dependency fails the suite rather than shipping open.
- **JWT (HS256) in two httpOnly cookies, `SameSite=Lax`.** `feedcast_access`
  (15 min, `Path=/`) authenticates requests; `feedcast_refresh` (7 days,
  `Path=/api/v1/auth`) only ever reaches the auth routes. `Secure` is
  `FEEDCAST_COOKIE_SECURE`, false only in `.env.local`. Each token carries a
  `type` claim, so neither can stand in for the other.
- **`/auth/refresh` rotates both tokens — and rotation is not revocation.**
  There is no allowlist or blocklist: a stolen refresh token stays valid until
  it expires, and logout only clears this browser's cookies. The kill switches
  are deleting the user row (every request re-loads the user) and rotating
  `FEEDCAST_JWT_SECRET`, which logs everyone out.
- **SameSite=Lax is the CSRF defence**, so GET endpoints must stay free of side
  effects — a cross-site top-level GET still carries the cookie.
- **Users are inserted by hand; there is no signup and no user CLI.**
  `password_hash` is an Argon2id PHC string, so any Argon2 tool can make one:

  ```bash
  docker compose exec api python -c "from argon2 import PasswordHasher; print(PasswordHasher().hash(input('password: ')))"
  docker compose exec db psql -U news2reel -c "insert into users (email, password_hash) values (lower('you@example.com'), '<hash>')"
  ```

  Emails are stored and matched lowercase — a check constraint refuses a
  mixed-case insert. A malformed hash fails the login
  closed rather than erroring, and an unknown email is verified against a dummy
  hash so the response time does not say which emails exist.
- **The UI refreshes silently.** `apiFetch` turns a 401 into one single-flight
  `POST /auth/refresh` and one retry; a failed refresh fires
  `feedcast:logged-out` and `main.js` routes to `#login`.
- **`#login` is the only route that renders logged out, and it is not in
  `ROUTES`.** Every route there fetches on render, so the gate in `fromHash()`
  sends any logged-out hash to `#login`, remembering it so logging in lands
  back on it; logged in, `#login` resolves to Discover. Its hero copy states
  what the tool does today — it finds, ranks and builds; it does not publish —
  so change it when that changes.

## Invariants (break these and something downstream breaks silently)

- **No files as interfaces.** State persists to Postgres, read over HTTP.
  `GET /api/v1/stories` is the contract phase 2's renderer and phase 3's editor
  UI both consume — version it, keep it stable.
- **A description is stored as the text it contained, not the markup it
  arrived in.** `core/text.py::strip_markup` runs at ingest, so the card, the
  sensitivity regex, the dedupe `content_hash` and the classifier's prompt all
  read prose. A description with no prose in it — Times of India's is often a
  CDATA block holding only an `<a><img/></a>` thumbnail — becomes NULL, because
  "we were given no summary" is the truth and an empty string is not. `raw`
  keeps what the feed actually said. Stored raw, those rows reached the model
  with `<img align="left" border="0" …>` in the summary slot: tokens spent on
  markup, and the story judged on its headline alone with nothing recording
  that.
- **`Article` is lenient, the story projection is strict.** Article mirrors
  messy feed reality. `StoryRead` is the render contract: if the renderer needs
  a field, it's required, or the story isn't render-eligible. There is no
  `stories` table — the projection is computed per request.
- **The `articles` table is the dedupe ledger, not a cache** — the PK *is* the
  URL+title hash and duplicates are skipped rather than inserted (`ON CONFLICT
  DO NOTHING` on the PK, so a tick racing a refresh cannot collide), so a row's
  *absence* is what makes a story eligible. Feeds carry only a recent window, so
  it cannot be rebuilt: truncating it resurrects old stories as new. Hence
  mandatory migrations, a backup (`docker compose exec db pg_dump` — **not yet
  automated**, and it should be), and dropping the DB never being the fix.
- **A city's feed is only news, and only about that city.** Two admission tests,
  ANDed, neither a rank weight and neither a badge: `is_city_relevant` for
  subject, `content_type` for form. Geography decided *whether* a story belongs
  here long before it decided where it sorts, and expressing it as a score meant
  national wire copy still appeared, just lower down. Neither has an override —
  a `local_only` toggle defaulting on is the same filter with a way to break the
  promise.
- **`content_type` asks what kind of article this is, never how important it
  is.** A dull story is still NEWS. "Minister reviews drainage work" and "onion
  worth ₹40,000 stolen" are NEWS, sunk by a low engagement score — not typed
  away. The moment a type means "this is minor", content_type and the score are
  answering the same question and will contradict each other, which is the
  double-count relevance-vs-impact is already fenced against. The test that
  keeps it honest: could a Pulitzer-winning version of this story carry the same
  type? If yes it is a form, if no it is an importance judgement in disguise.
  Form is also *why* this axis exists: relevance cannot answer it. Three
  near-identical lifestyle listicles from one source in one batch came back
  no @0.92, **yes @0.78**, no @0.92, because "is this about Pune" has no stable
  answer for a piece with no city in it. "Is this a tips listicle" has one.
- **The locality test has two branches, and the answer is never "everything".**
  Once the model has answered, it is fail-open: hidden iff `is_city_relevant IS
  FALSE AND relevance_confidence >= classify_confidence_floor`, so a story is
  dropped only when the model says no *and means it*. A low-confidence no keeps
  the story — confidence gates *removal*, never admission, and a hesitant "no" is
  not a cheap way out for the model. Before the model answers, the deterministic
  `derive_is_local` stands in: the Source is flagged `is_local_outlet`, or
  `Article.city_id` equals `Source.city_id`. The classifier runs on its own tick,
  and letting NULL mean "show it" made that tick a window in which a city's feed
  was simply everything its mastheads published — the one state the product
  exists to prevent, on display every time the editor hit Refresh. A weak
  provisional filter beats no filter, and it is provisional: the model overwrites
  it within `classify_tick_seconds`.
- **Ten fields, one call, four different jobs.** `category` filters, on the
  editor's terms. `is_city_relevant` admits on subject. `content_type` admits on
  form. The seven engagement dimensions rank, and they are one job, not seven.
  Every one of those jobs is a question the others cannot answer, and the
  recurring failure in this project is letting one creep into another's
  territory — scope was folded into the score once and had to be torn back out.
  Folding category into the score re-creates the editorialising the old LLM score
  axes were removed to avoid — the caller decides what a crime story is worth,
  not the server. Relevance never touches the score: it is a yes/no about the
  feed, and a story that fails it is not ranked low, it is absent. The score
  ranks because the deliverable is an Instagram post, and most of a local feed
  (a third of it is "minister reviews" politics) is unpostable. Category and the
  score are orthogonal on purpose: a crime story scores high or low depending on
  whether anyone would stop scrolling for it.
- **The engagement score predicts Instagram appeal, and it is derived, never
  stored.** It is a weighted sum over seven LLM-assigned dimensions —
  `emotional_salience` .20, `audience_breadth` .20, `impact` .20, `novelty` .15,
  `human_interest` .10, `timeliness` .10, `visual_potential` .05 — each a
  four-value enum mapped 0 / 0.33 / 0.67 / 1.0 in `rank.py`. Each level is stored
  as the enum the model returned; the combination is computed per request, so
  re-weighting takes effect immediately with no backfill. An article is classified
  once and never revisited inside the story window, so storing the collapsed
  number instead would make the weighting permanent. Weights sum to 1.0 and every
  component is 0–1; the sum is stretched onto 1–10, linearly and monotonically,
  so no ordering changes and the number on a card reads as a rating rather than a
  probability. **It asks how appealing the story would be as a post, not how much
  it asks of a resident** — that was the old single-axis question, and it sank
  exactly the wrong things: a water-cut notice is maximally actionable and makes a
  dull carousel, a tiger in a housing society is the reverse. Actionability
  survives as one dimension of seven, weighted .20. 16,384 combinations collapse
  to 441 distinct sums and **87 distinct scores at one decimal** — which is why
  the score is rounded to 1dp where three tiers needed no decimals at all.
- **There is no middle level, deliberately.** Four values, no midpoint, so the
  model has to pick a side. This is the direct fix for the shareability axis that
  badged 52% of the feed at its middle value. It also inverts the collapse
  signature: a no-middle enum fails by emptying its *extremes*, so an axis where
  `very_low + very_high` together are under ~10% has stopped measuring.
- **Scoring axes get tuned against a measured distribution, not a guess.** The
  shareability score this replaced badged 52% of the feed at its middle value
  because the model never spent a zero on one of its two axes — 3 articles in
  592. A new axis is not done until its distribution has been looked at: if one
  value is near-empty or one swallows the feed, the prompt has collapsed and the
  axis is not measuring what it claims. This binds the relevance boolean hardest,
  because it is not an axis but a gate: near-all-true and it is decorative,
  near-all-false and it empties the product, and both failures are silent.
- **Sources are archived, never hard-deleted** — Articles hold `source_id`
  forever, so a delete would cascade away history or trip an FK mid-fetch.
- **`sensitivity_flags` is rule-based only for now** — the LLM half went with
  the scoring pass. Regex can't be prompt-injected, so it's the safer half to
  be left holding it, but it is now single-sourced and an ordinary miss goes
  unflagged. Never fold it into a score.
- **Per-source failures never abort a scheduler tick** — record `last_error` on
  the Source, continue, report per-source outcomes.
- **The carousel endpoint looks stories up by PK with no window filter.** A
  selection lives in the browser and routinely outlives the feed window;
  re-applying the window there would make selections silently shrink between the
  feed and the canvas. `articles` outlives the window by design, so the lookup
  can and should.
- **The preview is the only renderer.** A slide is drawn to a `<canvas>` at
  1080×1350 and edited through a text field bound to it; what the editor sees is
  the exported pixels. Laying slides out in the DOM for editing and re-drawing
  them on canvas for export would be two implementations of one layout, free to
  drift — the same failure `project_story` exists to prevent, except the
  disagreement ships as an image.
- **Thumbnails are the same renderer, scaled.** The filmstrip draws each slide
  with `drawSlide` at full size and lets CSS shrink it, so a miniature cannot
  disagree with the preview. It spent one round as a DOM text index — more
  legible, since a 1080-wide slide at 8rem renders its headline at about 6px — and
  that was the wrong trade: the strip's job is showing *layout* at a glance,
  where the text sits and whether a slide is overfull, which only a miniature
  shows. Legibility lives in the inspector.
- **One type scale, named in `:root`.** `--text-sm` / `--text-base` / `--text-lg`,
  `--wordmark` and `--control-h` are the sizes the app already uses. A view
  drawn from a mock-up maps onto them rather than onto the mock-up's pixels:
  the login was first built at the mock's 18px text and 60px controls, and the
  wordmark visibly shrank the moment you logged in. The hero headline is the
  one deliberate display size.
- **A child that might be absent never goes straight into native `append`.**
  `el()`'s children array skips `null`; `Node.append` stringifies it, so a
  `cond && el(...)` child renders the literal word "null" on screen. Use `el()`'s
  children, or `.filter(Boolean)`. This has shipped once.
- **A deleted slide is gone.** There is no soft-removed state: a slide greyed in
  the strip is a third thing to reason about — in the deck, out of the deck, and
  in-the-deck-but-not-exported — bought for an undo that the confirmation
  dialog makes unnecessary.
- **A carousel is server state; `sessionStorage` is a per-tab cache in front of
  it.** The deck was browser-only for one sitting, on the argument that a
  carousels table was machinery with no second reader. The Gallery is that
  reader, so it reverses: every deck mutation funnels through `persist()`, which
  writes the cache immediately and PUTs after a ~1s pause. The cache is what
  keeps typing instant — never make a keystroke wait on a request — and the
  server is what the Gallery, and the next tab, read.
- **Exactly two places load a deck, and the canvas is neither.** Next on
  Discover builds a new carousel; a Gallery card opens an existing one; both
  then route to `#studio`, which only ever edits what it is handed. The canvas
  used to build one whenever it found no deck in `sessionStorage` — a proxy for
  "the editor just picked stories" that held only while a deck lived for one
  sitting. Once every deck is saved that condition is false from the second
  visit on, and Next silently reopened the previous carousel. Build on the
  click that means it, never on the absence of state.
- **Browser storage keys carry a version, and adding a field means bumping it.**
  The deck and the selection live in `sessionStorage`. A tab holding a deck
  from before a field existed rehydrates it,
  fills the new field with null, and renders as though the server never sent
  it — which looks exactly like a broken feature and is not. Verifying the API
  says nothing about what a returning tab is actually holding.
- **A carousel's state is derived from `last_downloaded_at`, never stored.**
  DOWNLOADED is `last_downloaded_at IS NOT NULL` and DRAFT is the absence of
  it, so the timestamp *is* the state machine and a `state` column would be a
  second copy free to drift from it. "Edited since download" is likewise just
  `last_edited_at > last_downloaded_at`. The API still returns a `state` string
  so no client re-implements the rule — the same shape as `score` serving a
  derived value. Only a streamed zip stamps the timestamp: the state has to mean
  a file actually reached someone.
- **`carousel_slides` is mutable working state; `selections` is frozen
  evidence.** Both name an article, for opposite reasons. Slides are edited,
  reordered and deleted, and what the feed badges as in-use reads from them so
  that deleting a slide clears the badge. `selections` records what the editor
  was *shown* at pick time, is written only by the two endpoints where someone
  picks, and is never touched by an autosave — an edit that could rewrite it
  would rewrite the history the engagement score is measured against. Never
  merge the two tables.
- **Deleting a carousel must not delete its evidence.** `carousel_slides` is
  `ON DELETE CASCADE`, `selections.carousel_id` is `ON DELETE SET NULL`. A
  tidy-up in the Gallery that quietly shrank the score's baseline would be
  undetectable and unrecoverable — feeds carry only a recent window, so the
  picks cannot be reconstructed.
- **The headline is the slide text.** No copy is written for it — see
  "deliberately removed". It goes out through `core/text.py::clip`: sanitised
  by the same policy as every other piece of feed text, then cut at a word
  boundary to `slide_text_max_chars`, which must equal `TEXT_MAX` in
  `slide-canvas.js` or the browser re-cuts it mid-word. On real data 3.6% of
  headlines are long enough to lose words, and those come back `clipped: true`
  and get badged on the canvas — a headline that quietly dropped its last words
  is the silent failure here, and the editor is the one who can fix it.
- **The story projection has one implementation**, `services/stories.py::project_story`.
  The score and the display city are derived, not stored, so a second copy would
  let the canvas disagree with the card the editor clicked. The admission test is
  deliberately *not* in it: `project_story` also serves the carousel lookup, which
  must return a story the feed has since hidden.
- **A human always approves before publish.** Nothing here publishes autonomously.

## Domain rules

- **Dedupe is `sha256(canonical_url + "\n" + normalized_title)`, global across
  sources, and it is the Article PK.** First source to deliver a story owns it;
  a corrected headline at the same URL becomes a new row. `canonicalize_url`
  strips tracking params first, or `utm_*` variants hash apart. No embeddings,
  no TF-IDF, no clustering — deliberately removed, see "out of scope".
- **Each source polls on its own `fetch_interval_minutes`**, floored by the
  response's `cache-control: max-age`, backing off `× 2^consecutive_failures` to
  a ceiling. `last_fetched_at` is stamped on *every* attempt — 304s and failures
  included — so a dead feed isn't re-hit every tick. Conditional GET keeps an
  early poll to a few hundred bytes; a 304 must still refresh the validators.
- **Two sort orders, chronological by default.** `sort=recent` orders on
  `published_at DESC`; `sort=engagement` orders on the engagement score alone,
  `(score, published_at) DESC`. No recency term and no date bucket in the
  engagement sort — it is the score, so a 47-hour-old story can top it. That is
  accepted: every card carries its publish time, and a decay rate is a free
  parameter there is no data to set. No feed-position term either: feeds are
  almost all ordered by publish time, so position repeats the date rather than
  adding to it. No image term: a photo says nothing about whether a story
  matters here. Nothing is persisted, so re-weighting needs no backfill.
- **Chronological stays the default until the score has been measured.** Under a
  date sort, position is uncorrelated with score, so which stories the editor
  picks is an unbiased read on whether the score works. Sorting by score first
  makes picks cluster at the top and turns that validation into a mirror — the
  same unfalsifiability that got the original LLM score axes deleted. Which is
  why `selections.sort` records the order the feed was in: a pick made under
  `engagement` is biased evidence about the score that reordered it, and has to
  stay separable. Flipping the default is a decision for after `selections` has
  data, not a tuning step.
- **`is_city_relevant` is the admission test**, LLM-assigned per article against
  the feed's city, with a 0–1 `relevance_confidence` beside it. The test is
  **primary subject**: the event, decision or development happened in, or is
  about, the city *or the district that city sits in*, or is a body of either
  acting. Its truth table is the two-branch invariant above: `true` or a
  sub-floor confidence show, a confident `false` hides, and `NULL` defers to
  `derive_is_local`.
- **The boundary is the city's own district, and it stops there.** A masthead's
  readership does not end at the municipal line — Shirur reads the Pune papers,
  Saoner the Nagpur ones — so a taluka in the same district is the city's news.
  The neighbouring district is not: Chandrapur, Akola and Gadchiroli are out of
  the Nagpur feed, and the state is out of every feed. Drawing the line at the
  district rather than the city widens *where* the boolean says yes; it does not
  make it a ladder, because the output is still one bit and nothing downstream
  ranks on distance. Resist the next step. A "how close is it" gradation is the
  scope enum returning, and it was deleted for being unfalsifiable.
- **State news is out even when it lands on a resident.** Maharashtra board exam
  dates, an MHT-CET rule change, a Supreme Court ruling on state policy — all
  rejected, however much a Pune parent has to act on them. "Does this affect a
  resident" is the `impact` dimension's question, and letting relevance ask it too
  is the same double-count the score's prompt already guards against. A city feed
  that admits state news is a state feed with a city bias.
- **`content_type` is the second admission test**, LLM-assigned, eight values:
  `news, opinion, advice_tips, promotional, celebrity_entertainment, explainer,
  oddity, other`. Discoverable is `{news, oddity}` — `discoverable_content_types`
  in config. `oddity` is in because a viral local curiosity makes good carousel
  content; `explainer` is out because "what the new UPI rules mean" is a service
  piece, not something that happened today. Everything else is junk for a feed
  whose job is "what happened here in the last 24 hours".
- **`is_discoverable` is derived, never stored** — `content_type in
  discoverable_content_types`, evaluated per request, so retuning the set takes
  effect on the next request with no backfill. Same reason the score and
  `carousel_state` are derived: a stored copy is a second truth free to drift.
  An unclassified `content_type` is **shown**, unlike unclassified locality,
  which has `derive_is_local` to fall back on. There is no cheap deterministic
  stand-in for "is this a listicle", and hiding NULLs would empty the feed on a
  classifier stall — the dead-scheduler alert all over again.
- **The content-type check runs before the locality branches in `_hidden`.** Both
  locality branches `return` early, so a check appended after them never runs for
  an unjudged row and a promo piece with a matching URL city sails through. Junk
  is junk wherever it happened.
- **A city is a row, and a source only exists inside one.** `City` holds a
  display `name` ("Nagpur"), an optional `state` ("Maharashtra") and a unique
  casefolded `slug` ("nagpur"); the slug is what `/city/<slug>/` URL segments
  match, the name is what people and the carousel's intro slide read. Cities are
  onboarded together with 1–`max_sources_per_city` feeds in one transaction, and
  a feed is only ever created under a city — there or through
  `POST /cities/{id}/sources`. There is no standalone `POST /sources`, because a
  city with no feeds is a city that silently produces nothing.
- **Archiving a city is reversible, and un-archiving cannot be precise.**
  `POST /cities/{id}/unarchive` mirrors the archive cascade: the city comes back
  and so does every one of its feeds. A row records *that* a feed was archived,
  never why, so this revives a feed someone retired by hand months before the
  city went away. The alternative is a column on `sources` existing only to serve
  undo; the cheaper answer is that archiving one feed again is one click. Pinned
  in `tests/test_cities_api.py` so it stays a decision rather than a surprise.
- **`CityRead.sources` carries archived feeds; `source_count` does not.** They
  answer different questions. The count is what the per-city cap is measured
  against, so a dead feed must not hold a slot. The list is what the admin page
  draws, and it needs the archived rows — cascade archiving stamps every feed, so
  a live-only list said nothing at all about an archived city and made one look
  like a city that never had a feed. Each row carries its own `archived_at`.
- **Refresh forces a poll of one city's sources, ignoring both the schedule and
  the backoff** (`POST /cities/{id}/refresh`, `scheduler.refresh_city`). It runs
  the same load/fetch/persist shape as a scheduled tick so the two cannot drift,
  keeps conditional GET (a 304 is a truthful "nothing new", and it keeps a
  refresh cheap for the publisher), and refuses an overlapping refresh for the
  same city — the button is one click away from a second round of requests at
  real mastheads.
- **A card shows a badge only for what the classifier has actually answered.**
  Category arrives on the classifier's own tick, so a story a refresh just pulled
  in has none and shows none — a badge reading "unknown" looks like a judgement
  about the story rather than a gap in our backlog. **Neither admission test is
  ever badged**, and neither reaches `StoryRead` at all: a story that failed one
  is not on screen, and one that passed would carry a chip reading "yes, this is
  about your city" or "yes, this is news" on every card in the feed. A badge
  whose value is the same on every row is furniture. The engagement score is not
  a badge either. Category is a fact about the story; the
  score is our judgement about the post, and as a third chip it read as one more
  attribute of equal standing. It renders as a band plus the raw 1–10 number on
  its own line, with a hover naming the two or three dimensions that actually
  drove *this* card's score — seven lines is a table, not a hover. Gated on
  `engagement`: an unclassified story still reaches the feed on the provisional
  test and has no levels, so it would otherwise print the floor as though that
  were a verdict. **`engagement` is one nullable object, not seven nullable
  fields, and that is the whole point of the shape** — the old gate was one
  column checked in four places with three different idioms, which worked only
  because that column happened to be null exactly when the story was unrated.
  Seven columns have no such column, and nominating one would make the gate a
  claim the schema cannot keep. A story is rated iff *all seven* levels are
  present and inside the enum; a partial row scored with its gaps at zero prints
  a confident low number with nothing saying it is a gap.
- **Archiving a city archives its sources.** The scheduler filters on the source,
  not the city, so an archived city whose feeds stayed active keeps being polled
  and keeps paying the classifier for a city nobody can select.
- **`is_local` is derived, not stored, and is only ever the provisional answer** —
  true when the Source is flagged `is_local_outlet`, or when `Article.city_id`
  (resolved at ingest from a `/city/<slug>/` URL segment) equals
  `Source.city_id`. The outlet flag exists because a genuinely local masthead
  need not put its city in its URLs at all; without it every article from one
  reads as non-local. It is a URL convention 78% of articles don't follow, so it
  is a poor test — but it is a *cheap* one, available at ingest, and the only
  thing standing between a refresh and an unfiltered feed until the classifier
  catches up. It never overrides the model: `_hidden` consults it only when
  `is_city_relevant IS NULL`. Deriving rather than storing means correcting a
  source's city fixes admission immediately with no re-ingest. It is not on
  `StoryRead` — it is an input to the filter, not something the editor acts on.
  Its other job is display: `is_local_outlet` supplies the shown `city` for a
  masthead whose URLs name none. A URL with no city segment still gets NULL
  rather than a fabricated city.
- **`Article.city` (the raw parsed slug) is kept alongside `Article.city_id`.**
  The id is NULL when the URL named a city that is not onboarded; the string is
  the ingested fact, and it is the only thing that lets a city added later be
  backfilled against articles already stored.
- **Switching city filters on `Source.city_id`, not the article's own city.**
  That selects everything the city's mastheads published; `is_city_relevant` is
  what narrows it to stories actually about the city. Filtering on the article's
  parsed city instead would hide every story whose URL carries no city segment —
  78% of them on real data — and would be a URL convention standing in for a
  judgement the model already makes.
- **Every *feed* is scoped to exactly one city; the Gallery is not.**
  `/stories` still accepts an absent `city_id` and answers with every city —
  that is the API's business, and phase 2 may want it — but Discover never asks
  that way. A mixed feed quietly breaks everything defined relative to a single
  city: relevance is judged against the *source's* city, so a mixed list is a
  union of separate admission tests rather than one feed; and the carousel's
  title already falls back to untitled for a mixed selection. The picker
  therefore always has a selection,
  and the feed refuses to render without one rather than widening itself.
  The Gallery is the exception because it is an archive, not a feed: nothing in
  it is defined relative to a city, every card names its own, and the work you
  are looking for is as likely to be in the city you covered last week. So it
  lists all of them and filters by city itself. The masthead picker belongs to
  Discover and does not reach it — a switcher that silently emptied an archive
  would read as lost work.
- **The 24h window is wall-clock** (`story_window_hours`), coalescing
  `published_at` with `fetched_at` — `published_at` is nullable and plenty of
  feeds omit it.
- **Category, both admission tests and all seven engagement dimensions come from
  one batched LLM call** over titles and summaries plus each item's feed city —
  relevance is defined relative to that city, so it cannot be judged without it,
  and appeal is hard to read off a headline alone (78% of articles carry a
  summary, and the RSS summary is the only snippet there is — no article body
  is fetched). Ten fields, one call: a second call would double the
  re-sent prompt, which is already the dominant cost.
  The seven are `emotional_salience`, `audience_breadth`, `impact`, `novelty`,
  `human_interest`, `timeliness` and `visual_potential`, each `very_low` /
  `low` / `high` / `very_high`, read relative to its own dimension. The prompt
  must not let locality raise any of them: every story that reaches a card
  already passed the relevance test, so rewarding locality again would just be
  relevance measured twice and noisily. `audience_breadth` is where that leaks
  in, which is why it counts people rather than area — see the out-of-scope note.
  Titles and summaries are untrusted, truncated and delimited.
- **The category enum is 16 values** — `crime, accident, civic, transport,
  politics, business, education, health, sport, weather, environment,
  agriculture, culture, religion, technology, other` — and the prompt spells out
  the three overlapping pairs, because the model reads the values off the JSON
  schema and nothing else: `transport` beats `civic` for metro, buses, traffic
  and railways; `environment` beats `weather` for pollution and tree-felling,
  leaving `weather` the forecast and its damage; `religion` beats `culture` for
  festivals and temples. Values that carry volume get a `.tag--<name>` colour;
  the rest fall through to the neutral base `.tag`, as `other` always has — so
  adding a value is no longer necessarily a renderer change, and the palette
  stays legible by staying small. The list is hand-copied in `core/enums.py` and
  `static/js/feed.js`; `tests/test_ui.py` pins the two together, because a
  browser-only mismatch shows up as a chip that filters nothing.
- **Only the common categories get a chip; the rest live behind "More".**
  Sixteen chips do not fit a row, and the row used to scroll horizontally, which
  is the worst of both — a filter past the fold may as well not exist and
  nothing says it is there. `PRIMARY_CATEGORIES` in `feed.js` is the inline
  eight, chosen by measured volume, and the disclosure is a native `<details>`
  so click, keyboard and screen-reader behaviour come with the element rather
  than a hand-rolled popover. Its summary carries a count when a hidden category
  is selected: a filter that is on but out of sight reads as a feed that has
  lost stories. The open state lives on the caller's state object, because
  Discover rebuilds the whole row on every pick.

## External input is hostile

Feeds, article text and image URLs are third-party and partly attacker-controlled.

- Fetch only through `core/net.py`: http/https only, private/link-local ranges
  rejected, no redirects into them, response bytes capped. There is one entry
  point, `fetch_url_async` — never add a second copy of that policy.
- Titles enter prompts as delimited, length-capped data. The response is
  constrained **twice**: a strict JSON schema on the API call fixes the shape,
  and pydantic revalidates the values. Both are needed — the schema can't reject
  an index for a title we never sent, and the fake client in tests is bound by
  no schema at all, so pydantic is what those exercise.
- Slide text is a feed headline, so it is third-party text on the way out as
  well as in: `core/text.py::sanitize` strips markup and control characters and
  turns `<`/`>` into lookalikes. It reaches `fillText` on a canvas, which is not
  HTML and cannot execute, but the editable field beside it is still
  `value`/`textContent`, never `innerHTML`.
- `POST /carousel/zip` takes rendered JPEGs back from the browser, so it is an
  upload boundary and capped like every other one: part count, declared type,
  per-file bytes and total bytes, all from `config.py` — `ZipRequest` derives
  its schema limits from those settings rather than restating them, which it
  once did, silently ignoring the setting.
- The UI is static ES modules under `app/static`, served by `StaticFiles` — no
  server-rendered HTML, so there is no template to escape. Instead: every URL
  reaching an `href`/`src` goes through `safeUrl()`, and every piece of API text
  reaches the DOM as `textContent` via `el()`. Never `innerHTML`. If HTML ever
  is rendered server-side, it goes through Starlette's `Jinja2Templates`
  (autoescape on, never a bare Jinja `Environment`).
- **Every response carries a strict CSP** (`main.py`): scripts, styles, images
  and connections from `'self'` only, plus Google Fonts; no inline script or
  style; `frame-ancestors 'none'`. It holds because the UI has no inline code,
  no style attributes and no third-party images — adding any of those means
  changing the policy, not loosening it quietly. `/docs` and `/openapi.json` are
  off unless `FEEDCAST_API_DOCS=true` (local only).

## Observability

Structured logs carry `stage` — `"scheduler"` for fetch ticks, `"classify"`
for classification passes. **Note they currently go nowhere**: nothing installs
a logging handler, so under `uvicorn` only uvicorn's own loggers print. Anything
that has to survive belongs in a table, not a log line. Every LLM call lands in
`LlmCall` with tokens and outcome; cost per day aggregates over `created_at`.
Per Source, `last_fetched_at`, `last_error` and `consecutive_failures` are the
ingestion health surface.

**`selections` is how the engagement score gets falsified.** One row per story
an editor put in a carousel, with the score and badge as shown. Compare those
against the window they were picked from — `articles` outlives the window, so
the baseline is always recomputable. Scores copied, never re-derived: reading
them back through today's weights would rewrite the history being measured.
**The table has two seams, and `score_version` marks them** — 1 for the two-axis
`scope + impact` score, 2 for impact alone, 3 for the seven dimensions. Nothing
may rewrite it, so a comparison spanning a seam is meaningless and has to be
split on that column. It is written from the row, never from a deploy constant:
the first post-deploy pick of a still-unrated article has no generation-3 score
at all and records NULL. Generation 1 exists only in the pre-2026-09-19 backups;
every row in the live table is 2 or later.

Three alerts that matter, all silent by nature: **`GET /stories` empty for
today's window** (scheduler loop died — the endpoint still answers 200), **the
unclassified backlog growing without bound** (classifier loop died — stories
still serve, but on the provisional test alone and with no content-type filter at
all, so the feed both narrows to whatever the URL convention admits and refills
with junk, and nothing errors), and **a city's feed emptying while articles keep
arriving** (a prompt collapsed to "no"). The last one is indistinguishable from
the first at the endpoint, and now has *two* possible causes, so the discriminator
is three counts, not two: rows in the window, rows the locality test admits, and
rows the content-type test admits. Only the pair tells you which prompt broke.
Measured once on 122 real in-window rows, the provisional test agrees with the
model on 90 of them and disagrees on 26: it wrongly admits 27 and wrongly hides
9. That is the price of the tick, and the reason it is provisional.

## Phase 2 constraints (decided, not yet built)

- **Render text in a browser, composite with ffmpeg** — `ffmpeg drawtext`
  mangles Indic conjuncts and ligatures. The carousel canvas already renders
  this way; video adds ffmpeg, not a second text renderer.
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

Do not add without discussion: signup / user registration, a token
allowlist or blocklist, login rate limiting, Redis, Celery/RQ or any
task queue (the two lifespan loops are the whole scheduler), multi-tenancy and
theming, epaper PDF ingestion, publishing APIs, the merged digest, on-screen
source attribution. Deliberately removed, don't reintroduce without discussion:
semantic clustering, embeddings and TF-IDF similarity; the run sequence and
per-run story rows; constrained lineup selection; **`location_scope` and every
graded geographic axis** — locality is a yes/no about admission, and grading it
put national wire copy on the page at a lower rank instead of off it; the
`local_only` toggle that went with it; LLM-written hooks — measured
at ~1,300 reasoning tokens and ~9.4s *per story* to produce one sentence, sitting
in the editor's critical path between "Next" and a usable canvas, for copy the
editor can retype in the textarea that is already open in front of them.
LLM-scored ranking axes came back, deliberately: the engagement score only,
because the product became Instagram content. What sank the originals was being
unfalsifiable, so the `selections` table is part of the feature, not a
follow-up — a new score axis without something that can prove it wrong belongs
in this list.

**`audience_breadth` counts people, not distance — that is what keeps it out of
the deleted scope axis.** `location_scope` graded proximity and ranked a named
street *above* a city-wide body; breadth ranks the city-wide body above the
street. On rows both would see they order the feed **inversely**, which is the
test: the day breadth agrees with proximity down the feed, it has collapsed and
the scope enum is back. Two structural differences hold it there — every value of
breadth describes an already-admitted story, so unlike scope it spends no
resolution on degrees of *not this city*; and it is a rank weight beside an
admission test that still runs, not in place of one. What was banned is geography
deciding where a story sorts *instead of* whether it belongs. The specific way it
collapses is Shirur and Saoner: the district rule admits them on purpose, and
marking a taluka narrow *for being a taluka* downranks exactly the rows that rule
exists to let in. Hence the prompt says people, not area, and says so twice.

**The seven dimensions are correlated, and the score is not seven-dimensional.**
`impact` and `audience_breadth` are .40 between them and both rise for a
city-wide outage; salience, human interest and novelty move together. That is not
the double-count fenced against above: those were *one question asked twice*,
where one asking admitted a story and the other ranked it, so a single fact did
both jobs. Correlated rank axes cannot contradict each other — they can only
misweight, and a misweight is a float in `config.py`. What correlation actually
costs is noise: the classifier agrees with itself only 65–75% on a tier, and axes
that move together concentrate that disagreement instead of averaging it out, so
the score wobbles far more than 87 reachable values implies. The number to look at
is therefore not the nominal weight but each axis's share of *score variance*, and
the pair test is whether an axis correlates with a different axis more than it
correlates with itself on a repeat run. An axis that fails that is not an axis.

## Conventions

- Type-hint everything; pydantic models for all API boundaries.
- Tunables (intervals, rank weights, batch sizes, concurrency, model names,
  timeouts, size caps) go in `core/config.py`, never inline.
- All LLM calls go through `services/llm.py` — retry with backoff, schema
  validation, usage logging. A classification failure leaves `category` null
  and is retried a bounded number of times; it never fails a fetch tick and
  never drops a story from `/stories`.
- **The classifier runs at `medium` reasoning effort, and the effort that is
  right depends on what you are asking for.** Filing a headline against fixed
  categories is not a reasoning task, and `minimal` was correct while the call
  returned four enum values: unset, the GPT-5 series averaged **44.6s and 5,480
  output tokens** over 276 real calls to do it, where minimal did the same 20
  headlines in **under 4s for ~710**, and lost nothing — `content_type` came
  back identical 20/20 at minimal over three runs, where `medium` agreed with
  *itself* only 90% of the time.
  **Grading is different, and the seven engagement dimensions broke it.**
  Measured on 40 real headlines: at `minimal` and at `low`, all seven axes
  return **zero `very_low` and zero `very_high`** — `novelty` was a single value
  for 100% of them. The model hedges into the two inner levels, which is the
  central-tendency collapse the no-middle enum was supposed to prevent; taking
  the midpoint out just moved the hedge to low/high. At `medium` every axis
  reaches both extremes on 10–25% of rows. Per 20-headline batch: minimal 7.7s /
  1,369 output tokens, low 4.3s / 705, medium 49.2s / 10,575 — at ~21 batches a
  day, cents, for a score that actually varies. Lowering it again without
  re-running the per-axis distribution buys back a score that ranks everything
  the same and never errors.
- **The classifier is noisy, and the noise is the floor on every axis.** Run the
  same headlines twice at any effort and category matches ~65–80%, relevance
  ~85%, impact ~65–75%. Only `content_type` is stable. So a change that moves one
  of those axes by a few points has moved nothing — measure any prompt change
  against a same-setting repeat run, never against a single previous run, or you
  will tune against a coin flip. `selections` exists for the same reason.
- Tests target the places where bugs are silent rather than loud:
  `scheduler.py` (due-time maths, backoff clamping), `ingest.py` (dedupe
  collisions, 304 handling, city parsing), `rank.py` (ordering), `classify.py`
  (batch index mapping — a mis-map mislabels everything without erroring).
