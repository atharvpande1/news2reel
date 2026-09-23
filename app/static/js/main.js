/* Routing. The hash is the only routing there is.
 *
 * Four views, three tabs: `studio` is a step inside the Discover flow rather
 * than a destination of its own, so it keeps Discover lit and gets no button.
 * The Gallery is a destination — it is where a carousel goes on living after
 * the tab that made it is gone.
 *
 * `#login` is the fifth route and the only one that renders logged out. It is
 * not in ROUTES on purpose: every route in there fetches on render, and the
 * gate in fromHash() is what keeps them from running without a session. */

import { renderFeed } from "./feed.js";
import { renderGallery } from "./gallery.js";
import { renderStudio } from "./studio.js";
import { renderCities } from "./cities.js";
import { size as deckSize } from "./slides.js";
import { mountPicker } from "./picker.js";
import { renderLogin } from "./login.js";
import { apiFetch } from "./api.js";

const ROUTES = {
  discover: { render: renderFeed, tab: "discover" },
  gallery: { render: renderGallery, tab: "gallery" },
  cities: { render: renderCities, tab: "cities" },
  studio: { render: renderStudio, tab: "discover" },
};

const root = document.getElementById("view");
const where = document.getElementById("where");
const buttons = document.querySelectorAll("[data-tab]");

function resolve(name) {
  if (!ROUTES[name]) return "discover";
  // The canvas edits a loaded deck and never builds one, so an empty deck is
  // simply nothing to show — a stale bookmark, or a carousel deleted from the
  // Gallery while it was open. The selection no longer counts here: it is
  // cleared the moment Next turns it into a deck, and testing it would send
  // someone to an empty canvas.
  if (name === "studio" && deckSize() === 0) return "discover";
  return name;
}

function render(name) {
  const route = ROUTES[name];
  for (const button of buttons) {
    button.setAttribute("aria-current", button.dataset.tab === route.tab ? "page" : "false");
  }
  // "Newest first" is deliberately not "ranked": the default sort is
  // chronological, and calling it ranked here would contradict the sort
  // toggle the moment someone opens Discover. See feed.js's sortToggle().
  if (name === "cities") where.textContent = "coverage and feed health";
  else if (name === "gallery") where.textContent = "every carousel, newest work first";
  else if (name === "studio") where.textContent = "edit and export the carousel";
  else where.textContent = "last 24 hours, newest first";
  // A per-route hook for CSS. The canvas is the one view that claims the whole
  // viewport and suppresses page scrolling, and it needs that to apply to body
  // — which no view module can reach without reaching outside itself.
  document.body.dataset.view = name;
  route.render(root);
}

/* Renders exactly once per call. An earlier version assigned to the hash and
 * then called render; the assignment re-fired hashchange, so every view
 * rendered twice — on the prompt step that is two POSTs per visit. Correcting
 * the hash here goes through replaceState precisely because that does *not*
 * fire hashchange. */
function fromHash() {
  const requested = window.location.hash.slice(1);
  if (authed === null) return; // /auth/me has not answered yet
  if (!authed) {
    // Remember where they were going, so logging in lands them there.
    if (ROUTES[requested]) returnTo = requested;
    if (requested !== "login") history.replaceState(null, "", "#login");
    showLogin();
    return;
  }
  // Logged in, `login` is not a route: resolve() sends it to Discover.
  const name = resolve(requested);
  if (requested !== name) history.replaceState(null, "", `#${name}`);
  render(name);
}

for (const button of buttons) {
  button.addEventListener("click", () => {
    const target = `#${button.dataset.tab}`;
    // Assigning fires hashchange, which renders. Re-clicking the active tab
    // fires nothing, so that case renders here instead — never both.
    if (window.location.hash === target) fromHash();
    else window.location.hash = target;
  });
}

/* The session gate. `authed` is null until /auth/me answers, and nothing
 * renders before then — the picker loads cities at mount and every view fetches
 * on render, so starting them logged out would paint a screen of 401s. */
const nav = document.querySelector(".nav");
const logout = document.getElementById("logout");
let authed = null;
let returnTo = "discover";
let pickerMounted = false;

function setChrome(loggedIn) {
  nav.hidden = !loggedIn;
  logout.hidden = !loggedIn;
  // The city picker owns its own `hidden`; CSS keys it off data-view="login".
}

function onLoggedIn() {
  authed = true;
  setChrome(true);
  // Once per page: a second mount would double every listener. Logging in
  // again after an expiry only needs the route redrawn.
  if (!pickerMounted) {
    pickerMounted = true;
    mountPicker();
  }
  history.replaceState(null, "", `#${returnTo}`);
  fromHash();
}

function showLogin() {
  setChrome(false);
  where.textContent = "";
  document.body.dataset.view = "login";
  renderLogin(root, onLoggedIn);
}

logout.addEventListener("click", async () => {
  await apiFetch("/auth/logout", { method: "POST" }).catch(() => {});
  // Straight to #login, and a reload so every module's in-memory state goes
  // with the session. replaceState, not assignment: no hashchange to race it.
  history.replaceState(null, "", "#login");
  window.location.reload();
});

// Session over mid-use (refresh token expired or revoked): back to #login,
// remembering the route so logging in returns to it.
window.addEventListener("feedcast:logged-out", () => {
  // Once per dead session: a view firing several requests gets several 401s,
  // and re-rendering the form for each would wipe what is being typed.
  if (authed === false) return;
  authed = false;
  fromHash();
});

window.addEventListener("hashchange", fromHash);

const requested = window.location.hash.slice(1);
if (ROUTES[requested]) returnTo = requested;
apiFetch("/auth/me").then(onLoggedIn, () => {
  authed = false;
  fromHash();
});
