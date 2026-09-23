/* The stories the editor has picked, and the only place that state lives.
 *
 * Deliberately outside feed.js: that module resets its own state on every
 * render, so a selection kept alongside it would empty itself the moment
 * someone changed a filter.
 *
 * Snapshots are stored, not bare ids, so the canvas and the tray can show a
 * story that has scrolled off the current filtered page without re-querying
 * for it. */

// v3: v2 snapshots carry a `score` from the single-impact formula, which had
// three reachable values. Rehydrated beside seven-dimension scores they would
// be ordered against a different scale — a tab that looks fine and ranks wrong.
const KEY = "feedcast.selection.v3";
const TITLE_CAP = 180;

/** Kept in step with settings.carousel_max_stories. Eight story slides plus an
 *  intro and a CTA is the ten-slide deck. The server is the real enforcer and
 *  answers 422; this only spares someone picking forty and finding out at the
 *  last step. */
export const MAX_SELECTED = 8;

const picked = new Map();
const listeners = new Set();

function snapshot(story) {
  return {
    id: story.id,
    title: String(story.title ?? "").slice(0, TITLE_CAP),
    source_name: String(story.source_name ?? ""),
    published_at: story.published_at ?? null,
    // Carried so ordered() can rank without re-querying. Unrated stories have
    // no score yet; they sort last rather than being dropped.
    score: typeof story.score === "number" ? story.score : 0,
  };
}

function isSnapshot(value) {
  return Boolean(value) && typeof value === "object" && typeof value.id === "string";
}

function persist() {
  try {
    sessionStorage.setItem(KEY, JSON.stringify([...picked.values()]));
  } catch {
    // Quota, or a private window that refuses storage. Selection keeps working
    // in memory; only surviving a reload is lost.
  }
}

function announce() {
  // Iterate a copy. A listener that re-subscribes while being notified — which
  // is exactly what a view does when it re-renders itself in response — would
  // otherwise be added to the Set mid-iteration, be visited by this same loop,
  // and recurse until the tab locks up.
  for (const listener of [...listeners]) listener();
}

function hydrate() {
  // This runs at module load, at the top of the import graph — anything thrown
  // here blanks the entire app, so a corrupt or foreign value must be dropped
  // rather than trusted.
  try {
    const stored = JSON.parse(sessionStorage.getItem(KEY) || "[]");
    if (!Array.isArray(stored)) return;
    for (const item of stored.slice(0, MAX_SELECTED)) {
      if (isSnapshot(item)) picked.set(item.id, snapshot(item));
    }
  } catch {
    picked.clear();
  }
}

hydrate();

/* Always handed out in the order the carousel will use, never click order.
 *
 * Engagement score descending, date breaking ties — the same key the server
 * orders the deck by, so the slide numbers on the feed cards agree with the
 * slides the canvas actually opens with. Deliberately *not* the feed's own
 * chronological default: a carousel is a ranked artefact, and the strongest
 * story earns the slide right after the intro whichever way the feed is sorted.
 * The server re-sorts authoritatively; this only keeps the numbers honest
 * before the response lands. */
function ordered() {
  return [...picked.values()].sort(
    (a, b) =>
      (b.score ?? 0) - (a.score ?? 0) ||
      Date.parse(b.published_at ?? 0) - Date.parse(a.published_at ?? 0),
  );
}

export const has = (id) => picked.has(id);
export const size = () => picked.size;
export const ids = () => ordered().map((story) => story.id);
export const items = () => ordered();
export const isFull = () => picked.size >= MAX_SELECTED;

/** Returns false when the story was rejected because the tray is full. */
export function toggle(story) {
  if (picked.has(story.id)) {
    picked.delete(story.id);
  } else {
    if (picked.size >= MAX_SELECTED) return false;
    picked.set(story.id, snapshot(story));
  }
  persist();
  announce();
  return true;
}

export function remove(id) {
  if (!picked.delete(id)) return;
  persist();
  announce();
}

export function clear() {
  if (picked.size === 0) return;
  picked.clear();
  persist();
  announce();
}

/** Returns an unsubscribe function. */
export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
