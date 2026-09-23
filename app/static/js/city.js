/* The city the editor is working in, and the only place that choice lives.
 *
 * localStorage, not sessionStorage: which city you cover is a standing fact
 * about you, not a fact about this tab. The story selection is the opposite,
 * which is why the two live in different stores.
 *
 * The list is fetched once and cached here so the picker, the feed and the
 * admin view all agree on it without three round trips. */

import { apiFetch } from "./api.js";

const KEY = "feedcast.city.v1";

let cities = [];
let selectedId = null;
let loaded = false;
let inFlight = null;
// Whether this page load began with a city already chosen. Captured before
// load() writes its own fallback over it — that fallback is why asking
// localStorage directly would say "yes" by the second visit no matter what the
// editor actually did.
let hadStoredSelection = false;
const listeners = new Set();

function readStored() {
  // Runs at module load, at the top of the import graph — anything thrown here
  // blanks the whole app, so a corrupt value is dropped rather than trusted.
  try {
    const raw = localStorage.getItem(KEY);
    const id = Number(raw);
    return Number.isInteger(id) && id > 0 ? id : null;
  } catch {
    return null;
  }
}

function persist() {
  try {
    if (selectedId === null) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, String(selectedId));
  } catch {
    // Quota, or a private window. The choice still holds for this page load.
  }
}

function announce() {
  // Iterate a copy. A listener that re-subscribes while being notified — which
  // is exactly what a view does when it re-renders itself in response — would
  // otherwise be added to the Set mid-iteration, be visited by this same loop,
  // and recurse until the tab locks up.
  for (const listener of [...listeners]) listener();
}

selectedId = readStored();
hadStoredSelection = selectedId !== null;

/** Fetches the city list and settles on a selection. Safe to call repeatedly;
 *  pass {force: true} after onboarding to pick up a new city. */
export async function load({ force = false } = {}) {
  if (loaded && !force) return cities;
  // One fetch however many callers: mountPicker() and renderFeed() both ask at
  // boot, and neither sets `loaded` until its own request returns — so without
  // sharing the promise a cold start makes two identical GET /cities calls.
  if (inFlight && !force) return inFlight;

  inFlight = (async () => {
    try {
      cities = await apiFetch("/cities");
      loaded = true;

      // A stored id whose city was archived — or that belongs to someone else's
      // database entirely — must not leave the editor staring at an empty feed
      // with no way to tell why.
      if (!cities.some((city) => city.id === selectedId)) {
        selectedId = cities.length > 0 ? cities[0].id : null;
        persist();
      }
      // Always announce: even when the selection held, the list itself may have
      // changed (a city onboarded or archived) and the picker renders from it.
      announce();
      return cities;
    } finally {
      inFlight = null;
    }
  })();

  return inFlight;
}

export const all = () => cities;
export const currentId = () => selectedId;
export const current = () => cities.find((city) => city.id === selectedId) ?? null;
export const isLoaded = () => loaded;

export function set(id) {
  // Flipped before the early return: picking the city that was already
  // highlighted is still the editor making the choice, and it should stop the
  // first-visit menu reopening just as much as switching would.
  hadStoredSelection = true;
  if (id === selectedId) return;
  selectedId = id;
  persist();
  announce();
}

/** True only on a visit that starts with no stored city AND more than one to
 *  choose between. A menu offering a single option is a dialog you dismiss. */
export const needsFirstChoice = () => !hadStoredSelection && cities.length > 1;

/** Marks the cached list stale so the next load() refetches — call after
 *  onboarding or archiving, so the picker does not lag the admin view. */
export function invalidate() {
  loaded = false;
}

/** Returns an unsubscribe function. */
export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
