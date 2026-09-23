/* Discover: the night's stories, ranked, and the tray you mark them into.
 *
 * This is a picking tool, so the whole card is the select target and reading
 * the original is a small deliberate link in the footer. */

import { apiFetch, clear, confirmDialog, el, isHealthy, safeUrl, strokeIcon, timeAgo } from "./api.js";
import * as city from "./city.js";
import * as selection from "./selection.js";
import * as slides from "./slides.js";

const PAGE_SIZE = 24;

/* The category enum, hardcoded on purpose and in enum order — this is exactly
 * the file that should have to be edited. tests/test_ui.py pins this list to
 * app/core/enums.py, because a mismatch shows up only as a chip that filters
 * nothing. Colour tokens live in css/app.css as .tag--<name>, and only the
 * values that carry volume have one; the rest fall through to the neutral base
 * .tag, as "other" always has. */
export const CATEGORIES = [
  "crime",
  "accident",
  "civic",
  "transport",
  "politics",
  "business",
  "education",
  "health",
  "sport",
  "weather",
  "environment",
  "agriculture",
  "culture",
  "religion",
  "technology",
  "other",
];

/* Multi-select: an empty Set means "All". Categories were single-select while
 * /stories already accepted a list — two identical-looking rows behaving
 * differently is the kind of thing nobody reports and everybody misreads.
 *
 * No locality filter of any kind. A city's feed is stories about that city and
 * the server admits them; there is nothing here to narrow and no toggle that
 * widens it back. See CLAUDE.md. */
const state = {
  categories: new Set(),
  // Whether the category row's "More" panel is open. Lives here, not on the
  // element, because the row is rebuilt on every pick — see chiprow().
  moreOpen: false,
  sort: "recent",
  offset: 0,
  sources: new Map(),
  notice: null,
};

// Dropped at the top of every render: renderFeed rebuilds the grid and tray the
// subscriber paints, so a listener left over from the previous render is one
// that updates detached nodes forever.
let unsubscribe = null;
let unsubscribeCity = null;
let refreshTicker = null;

// How long the refresh button stays disabled after a click — the rider-status
// "refresh" pattern: one tap, then a visible cooldown, so a fidgety second and
// third click cannot send more rounds of requests at real publishers before
// the first has even finished ranking. Keyed per city, not global: switching
// to a city that has not just been hit should not inherit another one's wait.
// Module-level and not persisted anywhere — it is a UI throttle, not a fact
// worth remembering past a reload.
const REFRESH_COOLDOWN_MS = 30_000;
// How long the button reads "Refreshed just now" before it starts counting
// down — a plain confirmation first, then the reason it is still disabled.
const REFRESH_GRACE_MS = 4_000;
const refreshCooldownUntil = new Map();

export function label(value) {
  return value.replace(/_/g, " ");
}

/** Which order the feed was in when the editor picked. Recorded against the
 *  selection, because a pick made under "Engagement score" came from a list the
 *  score had already reordered and is biased evidence about that same score. */
export function currentSort() {
  return state.sort;
}

function verifiedTick() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("class", "tick");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("fill-rule", "evenodd");
  path.setAttribute(
    "d",
    "M10 18a8 8 0 100-16 8 8 0 000 16zm3.857-9.809a.75.75 0 00-1.214-.882l-3.483 " +
      "4.79-1.88-1.88a.75.75 0 10-1.06 1.061l2.5 2.5a.75.75 0 001.137-.089l4-5.5z",
  );
  svg.append(path);
  const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
  title.textContent = "This feed is pulling cleanly";
  svg.append(title);
  return svg;
}

function openIcon() {
  return strokeIcon(["M7 4H4.5v11.5H16V13", "M11 4h5v5", "M16 4l-7 7"]);
}

// One glyph per filter row, matching the icon-then-label pattern the picker
// already uses elsewhere in the UI — a plain text label read fine, but this is
// what was asked for, and each is two or three strokes, not an icon library.
export function categoryIcon() {
  return strokeIcon(["M4 4.5h12", "M4 10h12", "M4 15.5h8"], 13);
}

export function sortIcon() {
  return strokeIcon(["M4 6h9", "M4 10h6", "M4 14h3", "M14 4v12", "M11 13l3 3 3-3"], 13);
}

export function clockIcon() {
  return strokeIcon(["M10 5.5V10l3 2", "M17.5 10a7.5 7.5 0 11-15 0 7.5 7.5 0 0115 0z"], 13);
}

export function scoreIcon() {
  return strokeIcon(["M5 15.5V11", "M10 15.5V6.5", "M15 15.5v-6"], 13);
}

/* The seven engagement dimensions and their weights, in weight order.
 *
 * Hand-copied from core/enums.py ENGAGEMENT_DIMENSIONS and the rank_weight_*
 * settings; tests/test_ui.py pins the names against the enum, for the same
 * reason it pins CATEGORIES — a mismatch here is browser-only and silent.
 *
 * The weights are duplicated rather than served because they are only used to
 * *order* the contributors in the hover, so a stale copy misranks an
 * explanation rather than misreporting a score. The score itself is always the
 * server's. */
export const ENGAGEMENT_DIMENSIONS = [
  ["emotional_salience", 0.2, "emotional pull"],
  ["audience_breadth", 0.2, "how many people it touches"],
  ["impact", 0.2, "real consequences"],
  ["novelty", 0.15, "how unusual it is"],
  ["human_interest", 0.1, "centres on people"],
  ["timeliness", 0.1, "needs seeing now"],
  ["visual_potential", 0.05, "makes a striking image"],
];

const LEVEL_WORD = {
  very_low: "very low",
  low: "low",
  high: "high",
  very_high: "very high",
};

/* Mirrors LEVEL_VALUES in services/rank.py. Used only for ordering the hover's
 * contributors — see the note on ENGAGEMENT_DIMENSIONS. */
const LEVEL_VALUES = { very_low: 0, low: 0.33, high: 0.67, very_high: 1 };

/* Poorest to best. A spectrum, not a rating widget: five buckets collapse most
 * of the score's 87 reachable values, so the emoji carries the band at a glance
 * and the number beside it carries what the band lost. */
export const ENGAGEMENT_EMOJI = ["\u{1F4A4}", "\u{1F610}", "\u{1F440}", "\u{1F525}", "\u{1F680}"];

/** 1-10 onto the five bands. Clamped, so the floor of the scale still gets an
 *  emoji rather than falling off the array and rendering "undefined". */
export function engagementBand(score) {
  return Math.min(5, Math.max(1, Math.round(score / 2)));
}

/** What the score is, and where this card's came from.
 *
 *  Per-card rather than one note on the page: a general explanation gives you
 *  the formula, this tells you why *this* story got a 7.8, which is the question
 *  actually being asked when someone reaches for the "?". */
export function scoreExplainer(story) {
  const top = topContributors(story.engagement);
  // Every dimension came back very_low, so there is no "mostly" to report —
  // the floor is the whole answer, and inventing a leading dimension for it
  // would name one the model actually rated as contributing nothing.
  const lead = top.length
    ? `Mostly: ${top
        .map(([name, , label]) => `${label} ${LEVEL_WORD[story.engagement[name]]}`)
        .join(", ")}.\n\n`
    : "Every dimension came back very low.\n\n";

  return (
    `Engagement score ${story.score.toFixed(1)} of 10.\n\n` +
    "How well this would work as a post, not how much it asks of you.\n\n" +
    lead +
    "Seven dimensions in all — emotional pull, reach, consequences, novelty, " +
    "human interest, timeliness and visual potential.\n" +
    "Location is not one of them: every story here is already about this city."
  );
}

/** The dimensions that actually drove this card's score, largest contribution
 *  first, at most three and never one that contributed nothing.
 *
 *  Seven lines is a table, not a hover — and the question someone reaches for
 *  the "?" to ask is why *this* story got a 7.8, not what the formula is.
 *  Ranked by weight x level, which is the same arithmetic the score is, read a
 *  different way. Filtered before slicing, or a very_low dimension could take a
 *  slot from a real contributor. */
function topContributors(engagement) {
  return ENGAGEMENT_DIMENSIONS.map((dim) => [dim, LEVEL_VALUES[engagement[dim[0]]] * dim[1]])
    .filter(([, contribution]) => contribution > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([dim]) => dim);
}

/** The score panel. Deliberately not a .tag — the category is a fact about the
 *  story, this is our judgement about the post, and as a second chip it read as
 *  one more attribute of equal standing.
 *
 *  Gated on `engagement`, not on the score: an unclassified story is shown (the
 *  feed is fail-open) and scores the floor, and printing that would read as a
 *  verdict rather than a gap in our backlog. One nullable object is the whole
 *  gate — see StoryRead. */
function engagementPanel(story) {
  if (!story.engagement) return null;
  const band = engagementBand(story.score);

  return el("div", { class: `card__rating card__rating--${band}`, title: scoreExplainer(story) }, [
    el("div", { class: "card__rating-row" }, [
      el("span", { class: "card__rating-emoji", "aria-hidden": "true", text: ENGAGEMENT_EMOJI[band - 1] }),
      el("span", { class: "card__rating-value", text: story.score.toFixed(1) }),
      el("span", { class: "card__rating-help", "aria-hidden": "true", text: "?" }),
    ]),
    el("span", { class: "card__rating-label", text: "Engagement score" }),
    el("span", { class: "sr-only", text: `Engagement score ${story.score.toFixed(1)} of 10` }),
  ]);
}

/** "Punekar News" -> "PN". Initials rather than a favicon: a masthead logo is a
 *  third-party request per card, and safeUrl() cannot vouch for what comes back. */
function sourceAvatar(name) {
  const initials = name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0].toUpperCase())
    .join("");
  return el("span", { class: "card__avatar", "aria-hidden": "true", text: initials });
}

/** The badges an article has actually earned — facts about the story, not our
 *  judgement of it. The engagement score is deliberately not one of them; it
 *  gets its own panel, see engagementPanel() above.
 *
 *  Category comes from the classifier, which runs on its own tick, so a story
 *  pulled in seconds ago has none and shows none, rather than one reading
 *  "unknown" — that looks like a judgement about the story instead of a gap in
 *  our backlog.
 *
 *  Relevance is never badged. A story that failed the admission test is not on
 *  screen at all, and one that passed would carry a chip saying "yes, this is
 *  about your city" on every card in a city feed. */
export function badges(story) {
  if (!story.category) return null;

  return el("div", { class: "card__tags" }, [
    el("span", { class: `tag tag--${story.category}`, text: label(story.category) }),
  ]);
}


/* What the editor has already done with this story.
 *
 * Informational, never a block: a follow-up on a story you covered yesterday is
 * a legitimate pick and only the editor can judge that. Absent entirely when no
 * deck holds it — most of the feed — so it costs nothing on a normal card.
 *
 * "downloaded" wins over "in_draft" server-side, because having already shipped
 * is the stronger claim on a story. */
const USED_LABEL = { downloaded: "downloaded", in_draft: "in a draft" };
const USED_TITLE = {
  downloaded: "This story is in a carousel that has been downloaded. Picking it again is allowed.",
  in_draft: "This story is in a draft carousel that has not been downloaded yet.",
};

export function usedBadge(story) {
  const used = story.carousel_state;
  if (!used) return null;
  return el("span", { class: `used used--${used}`, title: USED_TITLE[used] }, [
    strokeIcon(["M4 10.5l4 4 8-9"], 12),
    el("span", { text: USED_LABEL[used] }),
  ]);
}


function card(story, onToggle) {
  const node = el("article", {
    class: "card",
    role: "button",
    tabindex: "0",
    "aria-pressed": String(selection.has(story.id)),
  });

  // Inline rather than floated into a corner: the score panel owns the top
  // right now, and an absolutely positioned badge would sit on top of it.
  const slideNumber = el("span", { class: "card__slide", "aria-hidden": "true" });

  const href = safeUrl(story.url);
  const open = href
    ? el("a", {
        class: "card__open",
        href,
        target: "_blank",
        // Anti-tabnabbing, not decoration: the target page is third-party and
        // would otherwise get a handle on window.opener.
        rel: "noopener noreferrer",
        title: "Read the original",
        "aria-label": `Read the original: ${story.title}`,
        // The card is a select target; this is the one part of it that is not.
        onclick: (event) => event.stopPropagation(),
      })
    : null;
  if (open) open.append(openIcon());

  // .filter(Boolean), because this is the DOM's own append, not el()'s child
  // handling: native append stringifies a null into the literal text "null"
  // rather than skipping it, and several of these are null on a story the
  // classifier has not reached or a feed that sent no summary.
  const top = el("div", { class: "card__top" }, [
    el(
      "div",
      { class: "card__top-left" },
      [slideNumber, badges(story), usedBadge(story)].filter(Boolean),
    ),
    engagementPanel(story),
  ]);

  node.append(
    ...[
      top,
      el("h3", { class: "card__headline", text: story.title }),
      story.summary && el("p", { class: "card__summary", text: story.summary }),
      el("div", { class: "card__foot" }, [
        sourceAvatar(story.source_name),
        el("span", { class: "card__source", text: story.source_name }),
        isHealthy(state.sources.get(story.source_name)) ? verifiedTick() : null,
        el("span", { class: "card__when", text: timeAgo(story.published_at) }),
        open,
      ]),
    ].filter(Boolean),
  );

  const activate = () => onToggle(story, node);
  node.addEventListener("click", activate);
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      activate();
    }
  });
  return node;
}


/** "Refreshed just now" → "Refresh in 12s" → clickable again, entirely in the
 *  browser. Mirrors the pattern apps use on a rider-status refresh button. */
function refreshButton(root) {
  const button = el("button", { type: "button", class: "btn btn--small refresh-btn" });
  const spinner = el("span", { class: "spinner", hidden: true, "aria-hidden": "true" });
  const label = el("span", { "aria-live": "polite" });
  button.append(spinner, label);

  function paintIdle() {
    button.disabled = false;
    button.className = "btn btn--small refresh-btn";
    spinner.hidden = true;
    label.textContent = "Refresh";
  }

  function paintLoading() {
    button.disabled = true;
    button.className = "btn btn--small refresh-btn is-loading";
    spinner.hidden = false;
    label.textContent = "Fetching…";
  }

  function paintCooldown(remainingMs) {
    button.disabled = true;
    button.className = "btn btn--small refresh-btn is-cooldown";
    spinner.hidden = true;
    label.textContent =
      remainingMs > REFRESH_COOLDOWN_MS - REFRESH_GRACE_MS
        ? "Refreshed just now"
        : `Refresh in ${Math.ceil(remainingMs / 1000)}s`;
  }

  // Re-derives from the shared Map rather than trusting its own memory, so a
  // button rebuilt mid-cooldown (a filter change, a tab switch and back) picks
  // up exactly where the last one left off instead of resetting the wait.
  function tick(cityId) {
    const until = refreshCooldownUntil.get(cityId);
    const remaining = until ? until - Date.now() : 0;
    if (remaining <= 0) {
      refreshCooldownUntil.delete(cityId);
      paintIdle();
      if (refreshTicker) {
        clearInterval(refreshTicker);
        refreshTicker = null;
      }
      return;
    }
    paintCooldown(remaining);
  }

  const cityId = city.currentId();
  if (cityId !== null && refreshCooldownUntil.has(cityId)) {
    tick(cityId);
    refreshTicker = setInterval(() => tick(cityId), 1000);
  } else {
    paintIdle();
  }

  button.addEventListener("click", async () => {
    const activeCityId = city.currentId();
    if (activeCityId === null) {
      state.notice = { tone: "bad", text: "Pick a city before refreshing." };
      renderFeed(root);
      return;
    }
    paintLoading();
    try {
      const result = await apiFetch(`/cities/${activeCityId}/refresh`, { method: "POST" });
      state.notice = { tone: result.failed > 0 ? "bad" : "ok", text: describeRefresh(result) };
    } catch (error) {
      state.notice = { tone: "bad", text: `Could not refresh. ${error.message}` };
    }
    refreshCooldownUntil.set(activeCityId, Date.now() + REFRESH_COOLDOWN_MS);
    // Re-render either way: even a partly failed refresh may have brought
    // something in, and the feed should show it. The next refreshButton() call
    // picks the cooldown straight back up from the Map above.
    renderFeed(root);
  });

  return button;
}

/** icon + text, prefixing a filter row — matches the mockup's row labels
 *  without turning into an icon language of its own: three strokes each, drawn
 *  through strokeIcon() above. */
function filterLabel(icon, text) {
  return el("span", { class: "filter-label" }, [icon, el("span", { text })]);
}

/** One multi-select chip row over a Set. "All" is the empty Set rather than a
 *  separate flag, so there is no second piece of state to fall out of step. */
/* The categories an editor reaches for daily. Everything else in CATEGORIES
 * goes behind "More".
 *
 * Sixteen chips do not fit one row, and the row used to scroll horizontally —
 * which is the worst of both, because a filter past the fold may as well not
 * exist and nothing says it is there. Membership is by volume on real data,
 * not by taste: these are the values that actually carry stories. Transport is
 * here on arrival because it was carved out of civic, which was the fattest
 * bucket in the feed. */
const PRIMARY_CATEGORIES = new Set([
  "civic",
  "politics",
  "crime",
  "transport",
  "accident",
  "education",
  "business",
  "health",
]);

/** One multi-select chip row over a Set. "All" is the empty Set rather than a
 *  separate flag, so there is no second piece of state to fall out of step.
 *
 *  `ui` is optional and holds one thing: whether the More panel is open. It has
 *  to live outside the element because Discover rebuilds this whole row on
 *  every pick (onChange re-renders the feed), so the panel would otherwise snap
 *  shut the moment you chose something inside it. The story picker rebuilds
 *  only its list, so it passes nothing and the native state survives on its
 *  own. */
export function chiprow(icon, rowLabel, ariaLabel, values, selected, onChange, ui = null) {
  const group = el("div", { class: "chiprow", role: "group", "aria-label": ariaLabel });
  const chip = (text, value) =>
    el("button", {
      type: "button",
      class: "chip",
      text,
      "aria-pressed": String(value === null ? selected.size === 0 : selected.has(value)),
      onclick: () => {
        if (value === null) selected.clear();
        else if (selected.has(value)) selected.delete(value);
        else selected.add(value);
        onChange();
      },
    });

  const overflow = values.filter((value) => !PRIMARY_CATEGORIES.has(value));
  group.append(chip("All", null));
  for (const value of values.filter((value) => PRIMARY_CATEGORIES.has(value))) {
    group.append(chip(label(value), value));
  }

  if (overflow.length) {
    // <details>, not a hand-rolled popover: click, keyboard and screen-reader
    // behaviour come with the element, and there is no outside-click handler or
    // focus trap to get wrong.
    const chosen = overflow.filter((value) => selected.has(value)).length;
    const panel = el("div", { class: "chipmore__panel" });
    for (const value of overflow) panel.append(chip(label(value), value));
    // The count is the whole reason this is not just a "More" button: a filter
    // that is on but out of sight reads as a feed that has lost stories.
    const summary = el("summary", {
      class: chosen ? "chip chip--more chip--more-on" : "chip chip--more",
      text: chosen ? `More · ${chosen}` : "More",
    });
    const details = el("details", { class: "chipmore" }, [summary, panel]);
    if (ui) {
      details.open = Boolean(ui.moreOpen);
      details.addEventListener("toggle", () => {
        ui.moreOpen = details.open;
      });
    }
    group.append(details);
  }

  return el("div", { class: "filter-row" }, [filterLabel(icon, rowLabel), group]);
}

/** Newest / Engagement score.
 *
 *  Sorting is a server concern — the feed pages through /stories, so reordering
 *  here would only shuffle the page in hand. Newest is the default and stays
 *  that way: under a date sort, position is uncorrelated with the score, which
 *  is what makes the editor's picks evidence about whether the score works.
 *  See CLAUDE.md. */
export function sortToggle(sortState, onChange) {
  const group = el("div", { class: "chiprow chiprow--sort", role: "group", "aria-label": "Sort" });
  const chip = (icon, text, value) =>
    el("button", {
      type: "button",
      class: "chip chip--icon",
      "aria-pressed": String(sortState.sort === value),
      onclick: () => {
        if (sortState.sort === value) return;
        sortState.sort = value;
        onChange();
      },
    }, [icon, el("span", { text })]);
  const engagement = chip(scoreIcon(), "Engagement score", "engagement");
  engagement.title =
    "Ranks by how much each story asks of someone living here, highest first. " +
    "Hover any card's rating for its own breakdown.";
  group.append(chip(clockIcon(), "Newest", "recent"), engagement);
  return el("div", { class: "filter-row filter-row--sort" }, [
    filterLabel(sortIcon(), "Sort by"),
    group,
  ]);
}

function filters(root, onChange) {
  return el("div", { class: "filters" }, [
    chiprow(
      categoryIcon(),
      "Category",
      "Filter by category",
      CATEGORIES,
      state.categories,
      onChange,
      state,
    ),
    el("div", { class: "filters__row" }, [sortToggle(state, onChange), refreshButton(root)]),
  ]);
}

function describeRefresh(result) {
  if (result.sources_polled === 0) return "This city has no feeds to check.";

  const failed = result.sources.filter((source) => !source.ok);
  const brought =
    result.new_articles === 0
      ? "Nothing new"
      : `${result.new_articles} new ${result.new_articles === 1 ? "story" : "stories"}`;

  if (failed.length === 0) return `${brought}.`;
  // Name the feeds that failed. "1 of 4 feeds failed" just sends someone
  // hunting through the Cities page to find out which one.
  return `${brought}. Could not reach ${failed.map((source) => source.name).join(", ")}.`;
}

function emptyState() {
  // An empty window is a real failure signal, not just "nothing today": if the
  // scheduler loop has died this endpoint still answers 200 with [].
  return el("div", { class: "empty" }, [
    el("p", { class: "empty__head", text: "Nothing in the last 24 hours." }),
    el("p", {
      class: "empty__body",
      text:
        "Either this city's feeds have gone quiet, or ingestion has stopped. " +
        "Open Cities to check whether the feeds are pulling.",
    }),
  ]);
}

/** The city list could not be read and this browser had no remembered choice,
 *  so there is nothing to scope a feed to.
 *
 *  Deliberately not the empty-feed state: that one means ingestion has stopped,
 *  and borrowing it here would send someone hunting a problem they do not have. */
function cityUnavailableState(root, error) {
  return el("div", { class: "empty" }, [
    el("p", { class: "empty__head", text: "Could not load your cities." }),
    el("p", {
      class: "empty__body",
      text: error
        ? `The server said: ${error.message}`
        : "No city is selected, so there is nothing to show.",
    }),
    el("p", { class: "centered" }, [
      el("button", {
        type: "button",
        class: "btn btn--ink",
        text: "Try again",
        onclick: () => {
          city.invalidate();
          renderFeed(root);
        },
      }),
    ]),
  ]);
}

/** The cold start: no city exists, so there is nothing to have a feed of. */
function noCityState() {
  return el("div", { class: "empty" }, [
    el("p", { class: "empty__head", text: "No cities yet." }),
    el("p", {
      class: "empty__body",
      text: "Add the city you cover and the feeds you read, and stories will appear here.",
    }),
    el("p", { class: "centered" }, [
      el("button", {
        type: "button",
        class: "btn btn--ink",
        text: "Add a city",
        onclick: () => {
          window.location.hash = "cities";
        },
      }),
    ]),
  ]);
}

function tray() {
  const bar = el("div", { class: "tray", hidden: true });
  const count = el("span", { class: "tray__count" });
  const note = el("span", { class: "tray__note" });

  const inner = el("div", { class: "tray__inner" }, [
    count,
    note,
    el("span", { class: "tray__spacer" }),
    el("button", {
      type: "button",
      class: "btn btn--quiet btn--small",
      text: "Clear",
      onclick: () => selection.clear(),
    }),
    el("button", {
      type: "button",
      class: "btn btn--primary",
      text: "Next",
      // Confirmed, because this is the click that commits: it records the
      // selection, which is the only evidence the engagement score has. The
      // count is the point of the question — picking two when you meant eight
      // is a likelier mistake than clicking Next by accident.
      //
      // And this is where a carousel is born. The canvas used to build one
      // when it found no deck in session storage, which worked only while a
      // deck lived for one sitting — now that one is always saved, "no deck"
      // stopped meaning "the editor just picked stories" and Next reopened the
      // previous carousel instead of making a new one. Building here says what
      // is actually true: this click is the intent. The Gallery opens decks the
      // same way, so there are exactly two places a deck gets loaded and each
      // one names why.
      onclick: async () => {
        const n = selection.size();
        const ok = await confirmDialog({
          title: `Build a carousel from ${n} ${n === 1 ? "story" : "stories"}?`,
          body: "They'll become carousel slides, one headline each. You can still edit or drop any of them on the next screen.",
          confirmLabel: "Build the slides",
        });
        if (!ok) return;
        await buildCarousel(bar);
      },
    }),
  ]);
  bar.append(inner);

  function sync() {
    const n = selection.size();
    bar.hidden = n === 0;
    if (n === 0) return;
    clear(count);
    count.append(el("b", { text: String(n) }), document.createTextNode(n === 1 ? " story" : " stories"));
    note.textContent = selection.isFull() ? "That is the most a carousel can hold." : "";
  }

  sync();
  return { node: bar, sync };
}

/** The selection becomes a carousel, and the canvas opens on it.
 *
 *  Always a new one, even with a draft already open — the old draft is saved
 *  and waiting in the Gallery, so nothing is overwritten by starting another.
 */
async function buildCarousel(bar) {
  const note = bar.querySelector(".tray__note");
  note.textContent = "Building…";
  try {
    const result = await apiFetch("/carousels", {
      method: "POST",
      body: { story_ids: selection.ids(), sort: currentSort() },
    });
    const n = result.missing_ids.length;
    // The note rides along on the store: this view is about to be torn down,
    // and the canvas is where the editor will be when they read it.
    slides.load(
      result.id,
      result.slides,
      n ? `${n} selected ${n === 1 ? "story has" : "stories have"} gone; the rest are here.` : null,
    );
    // Consumed. Leaving the tray full would mean a second Next built a second
    // identical carousel — the footgun that "always a new carousel" creates,
    // and this is the only place worth closing it.
    selection.clear();
    window.location.hash = "studio";
  } catch (error) {
    note.textContent = "";
    state.notice = { tone: "bad", text: `Could not build the carousel. ${error.message}` };
    renderFeed(document.getElementById("view"));
  }
}

async function loadSources() {
  // include_archived: a story can outlive its source's archival, and an
  // unresolved name should render tickless rather than blow up.
  const sources = await apiFetch("/sources?include_archived=true");
  state.sources = new Map(sources.map((s) => [s.name, s]));
}

/** The one /stories query in the UI. The canvas's story picker builds the same
 *  filter rows and calls straight through to here — a second query shape is
 *  exactly how a picker ends up ignoring the filters it is showing, which is
 *  what it did before this was shared. */
export function fetchStories({ categories, sort, limit, offset = 0 }) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  // Repeated params, not comma-joined: /stories takes category as a Query list.
  for (const value of categories) params.append("category", value);
  if (sort !== "recent") params.append("sort", sort);
  // The city comes from the picker, not a filter box: switching city is
  // switching which newsroom you are working in, not narrowing a list. Always
  // sent — the feed refuses to render without one, because a feed mixing cities
  // undercuts everything defined relative to a single city: relevance is judged
  // against the source's city, and the carousel's own title needs one too.
  params.append("city_id", String(city.currentId()));
  return apiFetch(`/stories?${params}`);
}

function fetchPage() {
  return fetchStories({ ...state, limit: PAGE_SIZE, offset: state.offset });
}

export async function renderFeed(root) {
  if (unsubscribe) unsubscribe();
  if (unsubscribeCity) unsubscribeCity();
  if (refreshTicker) clearInterval(refreshTicker);
  clear(root);
  // Selection deliberately survives this reset — it lives in selection.js.
  state.offset = 0;

  const grid = el("div", { class: "grid" });
  const status = el("div", { class: "centered" });
  // Carried across the re-render a refresh triggers, then cleared — so the
  // result of the click survives the view being rebuilt but does not outlive it.
  const notice = state.notice;
  state.notice = null;
  const bar = tray();

  // Every selected card shows the slide number it will take in the carousel —
  // the order is real and the editor can be wrong about it.
  function paintSlideNumbers() {
    const order = selection.ids();
    for (const node of grid.querySelectorAll(".card")) {
      const position = order.indexOf(node.dataset.storyId);
      const chosen = position !== -1;
      node.setAttribute("aria-pressed", String(chosen));
      // Shown/hidden by CSS off the card's own aria-pressed, so there is no
      // second flag to keep in step with the selection.
      node.querySelector(".card__slide").textContent = chosen ? String(position + 1) : "";
    }
  }

  function onToggle(story) {
    // toggle() refuses only when the tray is full and this is an addition.
    if (!selection.toggle(story)) {
      status.replaceChildren(
        el("p", {
          class: "note note--bad",
          text: `A carousel holds ${selection.MAX_SELECTED} stories. Drop one to add another.`,
        }),
      );
    }
  }

  unsubscribe = selection.subscribe(() => {
    paintSlideNumbers();
    bar.sync();
  });

  root.append(filters(root, () => renderFeed(root)));
  if (notice) {
    root.append(el("p", { class: `note note--${notice.tone} feed__notice`, text: notice.text }));
  }
  root.append(grid, status, bar.node);

  const loadMore = el("button", {
    type: "button",
    class: "btn",
    text: "Load more",
    onclick: () => page(),
  });

  async function page({ first = false } = {}) {
    status.replaceChildren(el("span", { class: "note", text: "Loading…" }));
    try {
      const stories = await fetchPage();
      for (const story of stories) {
        const node = card(story, onToggle);
        node.dataset.storyId = story.id;
        grid.append(node);
      }
      state.offset += stories.length;
      paintSlideNumbers();
      clear(status);
      // No total count in the response, so a short page means the end.
      if (first && stories.length === 0) grid.replaceWith(emptyState());
      else if (stories.length === PAGE_SIZE) status.append(loadMore);
    } catch (error) {
      status.replaceChildren(
        el("p", { class: "note note--bad", text: `Could not load stories. ${error.message}` }),
      );
    }
  }

  let cityError = null;
  try {
    await city.load();
  } catch (error) {
    // Not fatal on its own: someone returning still has a city id in
    // localStorage and can be served from it. Only a first-time visitor is
    // actually stuck, and that case is caught below.
    cityError = error;
  }

  // Subscribed after the load, not before: load() announces, and a listener
  // installed first would be woken by the very call that set it up and
  // re-render on top of itself. Dropped at the top of each render, or every
  // city switch would leave another behind.
  unsubscribeCity = city.subscribe(() => {
    // Navigating away does not unsubscribe — only the next renderFeed does —
    // so this listener outlives the view. Without the guard, the Cities page
    // refreshing the city list would paint the feed straight over it.
    if (window.location.hash.slice(1) !== "discover") return;
    renderFeed(root);
  });

  if (city.isLoaded() && city.all().length === 0) {
    clear(root);
    root.append(noCityState());
    return;
  }

  if (city.currentId() === null) {
    clear(root);
    root.append(cityUnavailableState(root, cityError));
    return;
  }

  try {
    await loadSources();
  } catch {
    // The tick is an enhancement; a failed sources call must not empty the feed.
    state.sources = new Map();
  }
  await page({ first: true });
}
