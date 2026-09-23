/* The deck the editor is working on, and the cache in front of the server copy.
 *
 * Mirrors selection.js deliberately: module-level state, sessionStorage,
 * subscribe(). It used to end there — browser-only for one sitting, on the
 * argument that a carousels table was machinery with no second reader. The
 * Gallery is that reader, so this now writes through: sessionStorage
 * immediately, then a PUT once the typing stops.
 *
 * The cache is what keeps editing instant. A keystroke must never wait on a
 * request, and the canvas redraws from the field anyway — see studio.js.
 *
 * A deleted slide is gone. The soft-removed state it replaced was a third thing
 * to reason about — in the deck, out of it, and in-it-but-not-exported — bought
 * for an undo that the delete confirmation makes unnecessary. */

import { apiFetch } from "./api.js";
import { TEXT_MAX } from "./slide-canvas.js";

// v5: v4 slides carry a `location_scope` the inspector no longer renders and
// the API no longer sends, so one rehydrated here would keep a field nothing
// reads. v4 added the carousel id, without which every autosave was silently
// dropped; v3 replaced `generated` with `clipped`; v1/v2 predate
// url/category/score.
// v6: v5 decks hold slide scores from the single-impact formula, and an unrated
// story's slide held the floor 1.0 where it now holds null.
const KEY = "feedcast.slides.v6";

/* How long the typing has to stop before the deck goes to the server.
 *
 * Long enough that a sentence is one request rather than forty; short enough
 * that closing the laptop mid-thought loses a moment, not a slide. */
const SAVE_DEBOUNCE_MS = 1000;

/** Kept in step with the server: POST /carousels/{id}/zip accepts
 *  carousel_max_stories + 2 images, so a deck that grows past this would render
 *  fine and then fail at the one step that matters. */
export const MAX_SLIDES = 10;

let deck = [];
// Which carousel this deck *is*. Null before the first build, and the reason
// the key carries a version — a deck with no id has nowhere to save to.
let carouselId = null;
// When the deck last reached the server. Surfaced in the header, so the
// "auto-saved" line is reporting a real write rather than an assumption.
let savedAt = null;
let saveTimer = null;
/* A one-shot message from whoever loaded the deck to whoever renders it.
 *
 * The build happens on Discover and the result is read on the canvas, so
 * "two of your stories have gone" has to survive one hash change. Not
 * persisted: it is about the load that just happened, and a reload has nothing
 * left to say it about. */
let notice = null;
const listeners = new Set();

function clean(slide) {
  return {
    kind: slide.kind === "intro" || slide.kind === "cta" ? slide.kind : "story",
    story_id: typeof slide.story_id === "string" ? slide.story_id : null,
    text: String(slide.text ?? "").slice(0, TEXT_MAX),
    clipped: slide.clipped === true,
    source_name: typeof slide.source_name === "string" ? slide.source_name : null,
    // Carried for the inspector, absent on the brackets and on hand-written
    // slides. `?? null` rather than a type check: these come from our own API,
    // unlike the free-form text above.
    url: slide.url ?? null,
    category: slide.category ?? null,
    score: typeof slide.score === "number" ? slide.score : null,
  };
}

/* The single funnel. Every mutation below calls this — including setText,
 * which deliberately does not announce() because a repaint per keystroke used
 * to destroy the textarea being typed into. Putting the save here rather than
 * on announce() is what makes typing saved at all. */
function persist() {
  try {
    sessionStorage.setItem(KEY, JSON.stringify({ id: carouselId, deck }));
  } catch {
    // Quota, or a private window that refuses storage. Editing keeps working in
    // memory and still reaches the server; only surviving a reload is lost.
  }
  scheduleSave();
}

function scheduleSave() {
  // Nothing to save against until the first build has returned an id.
  if (carouselId === null) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, SAVE_DEBOUNCE_MS);
}

/** Last write wins, deliberately. Two tabs on one deck would need a version
 *  column and a 409, and there is one editor. */
async function save() {
  const id = carouselId;
  const body = {
    // Only what the server cannot recover from the article. Sending score back
    // would let a stale tab freeze an old rank weighting into the deck.
    slides: deck.map((slide) => ({
      kind: slide.kind,
      story_id: slide.story_id,
      text: slide.text,
      clipped: slide.clipped,
    })),
  };
  try {
    await apiFetch(`/carousels/${id}`, { method: "PUT", body });
    // Guarded: a Gallery click can swap the deck out from under an in-flight
    // save, and stamping "saved" then would be reporting the wrong carousel.
    if (id === carouselId) {
      savedAt = Date.now();
      announce();
    }
  } catch {
    // Left for the next mutation to retry. Nothing is lost — sessionStorage
    // already has it, and the editor is still looking at it.
  }
}

function announce() {
  // Iterate a copy — a listener that re-subscribes while being notified, which
  // is what a view does when it re-renders itself, would otherwise be visited
  // by this same loop and recurse until the tab locks up.
  for (const listener of [...listeners]) listener();
}

function hydrate() {
  // Runs at module load, at the top of the import graph — anything thrown here
  // blanks the entire app, so a corrupt or foreign value must be dropped.
  try {
    const stored = JSON.parse(sessionStorage.getItem(KEY) || "null");
    if (!stored || !Array.isArray(stored.deck)) return;
    deck = stored.deck.filter(Boolean).map(clean);
    carouselId = typeof stored.id === "number" ? stored.id : null;
  } catch {
    deck = [];
    carouselId = null;
  }
}

hydrate();

export const all = () => [...deck];
export const size = () => deck.length;
export const lastSavedAt = () => savedAt;
export const currentId = () => carouselId;

/** Replaces the deck wholesale — a fresh build, or a carousel opened from the
 *  Gallery. The id comes with it: a deck and the row it saves to are one thing,
 *  and setting them separately is how a tab ends up writing one carousel's
 *  edits into another. */
export function takeNotice() {
  const held = notice;
  notice = null;
  return held;
}

export function load(id, slides, note = null) {
  // Anything already queued belongs to the deck being replaced.
  clearTimeout(saveTimer);
  carouselId = id;
  deck = slides.map(clean);
  savedAt = null;
  notice = note;
  try {
    sessionStorage.setItem(KEY, JSON.stringify({ id: carouselId, deck }));
  } catch {
    // See persist().
  }
  announce();
}

export function clear() {
  if (deck.length === 0) return;
  clearTimeout(saveTimer);
  deck = [];
  carouselId = null;
  persist();
  announce();
}

export const room = () => Math.max(0, MAX_SLIDES - deck.length);

/** Appends a blank slide for the editor to write themselves. Returns its index,
 *  or null when the deck is already at the cap. */
export function add() {
  if (deck.length >= MAX_SLIDES) return null;
  // kind "story" rather than a fourth kind: it lays out like one, and the
  // canvas has no reason to care that no article is behind it. The absent
  // story_id is what the inspector reads to drop its source block.
  deck.push(clean({ kind: "story", story_id: null, text: "" }));
  persist();
  announce();
  return deck.length - 1;
}

export function setText(index, text) {
  const slide = deck[index];
  if (!slide) return;
  slide.text = String(text ?? "").slice(0, TEXT_MAX);
  // The badge said "this headline was cut short". Once it has been rewritten
  // that is no longer true, and a warning that outlives its cause is noise.
  slide.clipped = false;
  persist();
}

export function remove(index) {
  if (index < 0 || index >= deck.length) return;
  deck.splice(index, 1);
  persist();
  announce();
}

/** Reorder by moving one slide to another position. Indices are into the full
 *  deck, which is now the only list there is — nothing is hidden, so an index
 *  here and an index in the strip mean the same thing. */
export function move(from, to) {
  // Integer-checked, not just range-checked: NaN fails every comparison, so a
  // bare `to < 0 || to >= length` guard lets it through and splice(NaN) quietly
  // means splice(0) — a drop on a target with no index would move the slide to
  // the front instead of doing nothing.
  if (!Number.isInteger(from) || !Number.isInteger(to)) return;
  if (from === to) return;
  if (from < 0 || from >= deck.length || to < 0 || to >= deck.length) return;
  const [slide] = deck.splice(from, 1);
  deck.splice(to, 0, slide);
  persist();
  announce();
}

/** Returns an unsubscribe function. */
export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
