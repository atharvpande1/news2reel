/* The city switcher: the masthead button, and the menu behind it.
 *
 * Shaped like the city pickers people already know — a grid of tiles in a
 * modal, with a search box once there are enough cities to need one. No
 * "popular" vs "all" split and no A–Z headers: those exist because those apps
 * carry hundreds of cities, and a newsroom carries a handful.
 *
 * The menu renders from a snapshot and never subscribes to city.js. A listener
 * whose node is about to be removed is exactly the re-entrancy class that
 * announce()'s copy-iteration exists to survive — and with a modal that closes
 * on selection there is nothing to keep live anyway. */

import { clear, el } from "./api.js";
import * as city from "./city.js";

/** Below this the search box is furniture — scanning six tiles beats typing,
 *  and on a phone it steals the screen with a keyboard the moment it opens. */
const SEARCH_FROM = 8;

const button = document.getElementById("city-picker");
const label = document.getElementById("city-name");

function matches(record, query) {
  if (!query) return true;
  // Name and state both, so "maha" finds everything in Maharashtra.
  return `${record.name} ${record.state ?? ""}`.toLowerCase().includes(query);
}

function tile(record, onPick) {
  const selected = record.id === city.currentId();
  return el(
    "button",
    {
      type: "button",
      class: "citytile",
      // Same "this one is chosen" language the story cards use.
      "aria-pressed": String(selected),
      onclick: () => onPick(record),
    },
    [
      el("span", { class: "citytile__name", text: record.name }),
      // Absent rather than empty: state is optional at onboarding and most
      // cities have none, so the tile has to look deliberate without one.
      record.state && el("span", { class: "citytile__state", text: record.state }),
      // Shown only when it is bad news. A count of four predicts nothing much,
      // but a count of zero predicts exactly what switching here will get you:
      // an empty feed. Worth saying before the click, not after.
      record.source_count === 0 && el("span", { class: "citytile__warn", text: "No feeds" }),
      // The tint and border marking the current city are both colour. This is
      // the same fact in a form that survives not seeing colour.
      selected && el("span", { class: "citytile__now", text: "Current" }),
    ],
  );
}

function openMenu() {
  // A double click, or a click racing the auto-open, would otherwise showModal()
  // a second dialog over the first and strand it in the top layer.
  if (document.querySelector("dialog.citymenu[open]")) return;
  if (city.all().length === 0) return;

  const dialog = el("dialog", { class: "dialog--wide citymenu" });
  const grid = el("div", { class: "citygrid", role: "group", "aria-label": "Cities" });
  const records = city.all();

  const choose = (record) => {
    // Only city.set(). The feed has its own subscriber and re-renders itself;
    // nudging it from here too would mean two renders and two /stories calls.
    city.set(record.id);
    dialog.close();
  };

  function shownFor(query) {
    return records.filter((record) => matches(record, query));
  }

  function paintGrid(query) {
    clear(grid);
    const shown = shownFor(query);
    if (shown.length === 0) {
      // A line, not a blank grid: an empty box reads as broken rather than as
      // "nothing matched".
      grid.append(el("p", { class: "citygrid__empty", text: `No city matches “${query}”.` }));
      return;
    }
    for (const record of shown) grid.append(tile(record, choose));
  }

  const head = el("div", { class: "citypicker__head" }, [
    el("h2", { class: "panel__title", text: "Select your city" }),
    el("span", { class: "tray__spacer" }),
    el("button", {
      type: "button",
      class: "citypicker__close",
      text: "✕",
      "aria-label": "Close",
      onclick: () => dialog.close(),
    }),
  ]);

  const body = el("div", { class: "citypicker__body" }, [grid]);

  // Autofocus only where a keyboard is already present. On a phone it throws
  // the on-screen keyboard over the grid the editor opened this to look at.
  const pointerFine = window.matchMedia("(pointer: fine)").matches;
  let search = null;
  if (records.length > SEARCH_FROM) {
    search = el("input", {
      type: "search",
      class: "citysearch",
      placeholder: "Search for your city",
      "aria-label": "Search for your city",
      autofocus: pointerFine ? "autofocus" : null,
    });
    // Only the grid is repainted, never the input: rebuilding it on each
    // keystroke would drop focus and the caret with it.
    search.addEventListener("input", () => paintGrid(search.value.trim().toLowerCase()));
    search.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const shown = shownFor(search.value.trim().toLowerCase());
      // Only when the filter has narrowed to one. Guessing between several is
      // worse than doing nothing.
      if (shown.length === 1) choose(shown[0]);
    });
    body.prepend(search);
  }

  paintGrid("");

  // Escape-dismissal fires `close` and nothing else, so removing the node here
  // is what stops every open leaving a detached <dialog> behind in the body.
  dialog.addEventListener("close", () => {
    dialog.remove();
    // Native focus restoration has nothing to restore when the menu opened
    // itself rather than being clicked open.
    if (!button.hidden) button.focus();
  });
  // Light dismiss: a click on the backdrop targets the dialog element itself,
  // whereas a click on anything inside targets that child.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  dialog.append(head, body);
  document.body.append(dialog);
  dialog.showModal();

  if (!search || !pointerFine) {
    grid.querySelector('.citytile[aria-pressed="true"]')?.focus();
  }
}

function paint() {
  const current = city.current();
  // Hidden only when there is genuinely nothing to name. With one city the
  // button still earns its place: it says which city the feed *is*, which is
  // worth stating whether or not there is anywhere else to go.
  button.hidden = current === null;
  label.textContent = current ? current.name : "";
}

export function mountPicker() {
  button.addEventListener("click", openMenu);
  city.subscribe(paint);
  // The list arrives asynchronously; paint now so the button does not flash in
  // empty, and again once it lands.
  paint();
  city
    .load()
    .then(() => {
      paint();
      // Once, at mount, never from inside a subscriber — and not while the
      // editor is on the Cities tab, where being asked to pick a city in the
      // middle of adding cities is an interruption rather than a prompt.
      if (!city.needsFirstChoice()) return;
      if (window.location.hash.slice(1) === "cities") return;
      // A macrotask, so every queued microtask runs first. renderFeed() awaits
      // the same shared load() promise and registered its .then after ours, so
      // calling openMenu() inline could put the menu on screen before the feed
      // has installed the city subscriber that reacts to a choice.
      setTimeout(openMenu, 0);
    })
    .catch(() => paint());
}
