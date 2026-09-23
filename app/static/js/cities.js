/* Cities: the coverage areas, each with the feeds it was onboarded with.
 *
 * A source cannot exist outside a city, so this is the only place either gets
 * created — there is no standalone "add a feed" anywhere in the app.
 *
 * The page is an admin surface rather than a reading one: search and filter at
 * the top, one collapsible card per city, and a table of that city's feeds with
 * the health columns underneath. Two mechanics here are easy to get subtly
 * wrong and are marked below: buttons inside a <summary> toggle the disclosure
 * unless they say otherwise, and open state has to survive the full re-render
 * every mutation triggers. */

import { apiFetch, clear, confirmDialog, el, safeUrl, strokeIcon, timeAgo } from "./api.js";
import * as city from "./city.js";
import {
  FIELDS,
  field,
  health,
  healthState,
  lastCheckedAt,
  openSourceDialog,
  readForm,
  runCheck,
} from "./sources.js";

/** Mirrors settings.max_sources_per_city. The server is the real enforcer and
 *  answers 422; this only stops someone filling in a seventh row and finding
 *  out on submit. */
const MAX_SOURCES = 5;

/* Survives the re-render. Every mutation on this page rebuilds it from
 * scratch, so anything kept on a DOM node instead of here — which card is open,
 * what was typed in the search box — resets under the user mid-task. Same
 * reason feed.js keeps its "More" panel's open state on a state object. */
const state = {
  query: "",
  filter: "all",
  open: new Set(),
};

const FILTERS = [
  ["all", "All"],
  ["active", "Active"],
  ["archived", "Archived"],
];

function searchIcon() {
  return strokeIcon(["M9 15a6 6 0 100-12 6 6 0 000 12z", "M13.6 13.6L17 17"], 15);
}

function chevronIcon() {
  return strokeIcon(["M7.5 5l5 5-5 5"], 16);
}

function plusIcon() {
  return strokeIcon(["M10 4v12", "M4 10h12"], 15);
}

function moreIcon() {
  return strokeIcon(["M10 5.2v.1", "M10 9.95v.1", "M10 14.7v.1"], 16);
}

/* ---------- onboarding dialog ---------- */

function cityFields(record) {
  return [
    el("label", { class: "block" }, [
      el("span", { class: "form__label", text: "City" }),
      el("input", {
        name: "name",
        class: "form__input",
        placeholder: "Nagpur",
        required: "required",
        value: record ? record.name : "",
      }),
    ]),
    el("label", { class: "block" }, [
      el("span", { class: "form__label", text: "State (optional)" }),
      el("input", {
        name: "state",
        class: "form__input",
        placeholder: "Maharashtra",
        value: record && record.state ? record.state : "",
      }),
    ]),
  ];
}

function sourceRowFields(index, onRemove) {
  const head = el("div", { class: "subform__head" }, [
    el("span", { class: "subform__n", text: `Feed ${index + 1}` }),
    el("span", { class: "tray__spacer" }),
    onRemove &&
      el("button", {
        type: "button",
        class: "btn btn--quiet btn--small",
        text: "Remove",
        onclick: onRemove,
      }),
  ]);
  return el("fieldset", { class: "subform", dataset: { sourceIndex: String(index) } }, [
    head,
    ...FIELDS.map((spec) => field({ ...spec, name: `s${index}_${spec.name}` }, undefined)),
  ]);
}

function openOnboardDialog({ onSaved }) {
  const dialog = el("dialog", { class: "dialog--wide" });
  const error = el("p", { class: "note note--bad" });
  const rows = el("div", { class: "stack" });
  const hint = el("p", { class: "note" });

  let nextIndex = 0;
  const live = new Set();

  function syncHint() {
    hint.textContent = `${live.size} of ${MAX_SOURCES} feeds`;
    addButton.disabled = live.size >= MAX_SOURCES;
  }

  function addRow() {
    if (live.size >= MAX_SOURCES) return;
    const index = nextIndex++;
    // Removable only while more than one remains: a city with no feeds produces
    // nothing and reports nothing, so the form will not let you build one.
    const node = sourceRowFields(index, () => {
      if (live.size <= 1) return;
      live.delete(index);
      node.remove();
      syncHint();
    });
    live.add(index);
    rows.append(node);
    syncHint();
  }

  const addButton = el("button", {
    type: "button",
    class: "btn btn--small",
    text: "Add another feed",
    onclick: addRow,
  });

  const form = el("form", { class: "form" }, [
    el("h2", { class: "panel__title", text: "Onboard a city" }),
    el("p", {
      class: "note",
      text: "A city is added together with the feeds it pulls from — it needs at least one.",
    }),
    ...cityFields(null),
    rows,
    el("div", { class: "filters" }, [hint, el("span", { class: "tray__spacer" }), addButton]),
    error,
    el("div", { class: "form__actions" }, [
      el("button", {
        type: "button",
        class: "btn btn--quiet",
        text: "Cancel",
        onclick: () => dialog.close(),
      }),
      el("button", { type: "submit", class: "btn btn--ink", text: "Add city" }),
    ]),
  ]);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clear(error);
    const body = {
      name: form.elements.name.value.trim(),
      state: form.elements.state.value.trim() || null,
      sources: [...live].map((index) => readForm(form, FIELDS, `s${index}_`)),
    };
    try {
      const saved = await apiFetch("/cities", { method: "POST", body });
      dialog.close();
      onSaved(saved);
    } catch (err) {
      error.textContent = err.message;
    }
  });

  // One removal path for every way out — Cancel, Escape and the backdrop all
  // fire close. The old code removed it only on a successful submit, so every
  // cancel left a detached <dialog> behind.
  dialog.addEventListener("close", () => dialog.remove());

  dialog.append(form);
  document.body.append(dialog);
  dialog.showModal();
  addRow();
}

/* ---------- the feeds table ---------- */

function overflowMenu(source, { reload }) {
  const href = safeUrl(source.feed_url);
  const items = el("div", { class: "rowmenu__panel" }, [
    href &&
      el("a", {
        class: "rowmenu__item",
        href,
        target: "_blank",
        rel: "noopener noreferrer",
        text: "Open feed ↗",
      }),
    el("button", {
      type: "button",
      class: "rowmenu__item rowmenu__item--bad",
      text: "Archive feed",
      onclick: async () => {
        const ok = await confirmDialog({
          title: `Archive ${source.name}?`,
          body:
            "It stops being checked for new stories. Everything it has already " +
            "collected is kept — feeds are archived, never deleted.",
          confirmLabel: "Archive feed",
        });
        if (!ok) return;
        await apiFetch(`/sources/${source.id}/archive`, { method: "POST" });
        reload();
      },
    }),
  ]);

  const summary = el("summary", { class: "rowmenu__toggle", title: "More actions" }, [moreIcon()]);
  summary.setAttribute("aria-label", `More actions for ${source.name}`);
  return el("details", { class: "rowmenu" }, [summary, items]);
}

function feedRow(source, { reload, onEdit }) {
  // Tagged rather than found by class afterwards — several siblings share the
  // same utility classes, so a querySelector would grab the wrong one.
  const checkSlot = el("span", { class: "feeds__check", dataset: { checkSlot: String(source.id) } });
  const href = safeUrl(source.feed_url);
  const checked = lastCheckedAt(source);

  return el("tr", { class: "feeds__row" }, [
    el("td", { class: "feeds__name" }, [
      el("span", { text: source.name }),
      source.is_local_outlet && el("span", { class: "feeds__flag", text: "Local outlet" }),
    ]),
    el("td", { class: "feeds__url" }, [
      href
        ? el("a", { href, target: "_blank", rel: "noopener noreferrer", text: source.feed_url })
        : el("span", { text: source.feed_url }),
    ]),
    el("td", {}, [health(source), checkSlot]),
    el("td", { class: "feeds__num", text: `${source.fetch_interval_minutes} min` }),
    el("td", { class: "feeds__num", text: checked ? timeAgo(checked) : "never" }),
    el("td", {}, [
      el("div", { class: "feeds__actions" }, [
        el("button", {
          type: "button",
          class: "btn btn--small",
          text: "Check now",
          onclick: () => runCheck(source.id, checkSlot),
        }),
        el("button", {
          type: "button",
          class: "btn btn--small",
          text: "Edit",
          onclick: () => onEdit(source),
        }),
        overflowMenu(source, { reload }),
      ]),
    ]),
  ]);
}

const COLUMNS = ["Source", "URL", "Status", "Check interval", "Last checked", "Actions"];

function feedsTable(live, { reload, onEdit }) {
  return el("table", { class: "feeds" }, [
    el("thead", {}, [
      el(
        "tr",
        {},
        COLUMNS.map((name) => el("th", { scope: "col", text: name })),
      ),
    ]),
    el(
      "tbody",
      {},
      live.map((source) => feedRow(source, { reload, onEdit })),
    ),
  ]);
}

/* ---------- one city ---------- */

function cityCard(record, { reload }) {
  const live = record.sources.filter((source) => !source.archived_at);
  const archived = record.sources.length - live.length;
  const healthy = live.filter((source) => healthState(source) === "ok").length;
  const isArchived = Boolean(record.archived_at);
  const remaining = MAX_SOURCES - record.source_count;

  /* Buttons inside a <summary> toggle the disclosure, because toggling is the
   * summary's default action for a click anywhere inside it. preventDefault on
   * the button's own click suppresses exactly that and nothing else. Without
   * this, opening the Add feed dialog also collapses the card behind it. */
  const act = (fn) => (event) => {
    event.preventDefault();
    event.stopPropagation();
    fn();
  };

  const onEdit = (source) =>
    openSourceDialog({
      title: `Edit ${source.name}`,
      source,
      onSaved: (saved) => reload(saved),
    });

  const addFeed = el("button", {
    type: "button",
    class: "btn btn--small",
    text: "Add feed",
    onclick: act(() =>
      openSourceDialog({
        title: `Add a feed to ${record.name}`,
        submit: (body) => apiFetch(`/cities/${record.id}/sources`, { method: "POST", body }),
        onSaved: (saved, opts) => reload(saved, opts),
      }),
    ),
  });
  if (remaining <= 0) {
    addFeed.disabled = true;
    addFeed.title = `${record.name} already has ${MAX_SOURCES} feeds.`;
  }

  const archiveButton = el("button", {
    type: "button",
    class: "btn btn--small",
    text: isArchived ? "Unarchive" : "Archive",
    onclick: act(async () => {
      if (isArchived) {
        await apiFetch(`/cities/${record.id}/unarchive`, { method: "POST" });
        reload();
        return;
      }
      const ok = await confirmDialog({
        title: `Archive ${record.name}?`,
        body:
          `Its ${record.source_count} feed(s) stop being checked for new stories. ` +
          "Nothing already collected is deleted, and you can bring it back.",
        confirmLabel: "Archive city",
      });
      if (!ok) return;
      await apiFetch(`/cities/${record.id}/archive`, { method: "POST" });
      reload();
    }),
  });

  const summary = el("summary", { class: "citycard__head" }, [
    el("span", { class: "citycard__chevron", "aria-hidden": "true" }, [chevronIcon()]),
    el("span", { class: "citycard__name", text: record.name }),
    record.state && el("span", { class: "citycard__state", text: record.state }),
    el("span", {
      class: `health health--${isArchived ? "off" : "ok"}`,
      text: isArchived ? "Archived" : "Active",
    }),
    el("span", { class: "citycard__gap" }),
    el("span", {
      class: "citycard__count",
      text: `${healthy} of ${record.sources.length} feed${record.sources.length === 1 ? "" : "s"}`,
      title: "Feeds pulling cleanly, of every feed this city has.",
    }),
    addFeed,
    archiveButton,
  ]);

  const body = el("div", { class: "citycard__body" }, [
    live.length
      ? feedsTable(live, { reload, onEdit })
      : el("p", { class: "note citycard__none", text: "No active feeds." }),
    archived > 0 &&
      el("p", {
        class: "note citycard__archived",
        text: `${archived} archived feed${archived === 1 ? "" : "s"}, not shown.`,
      }),
  ]);

  const card = el("details", { class: "citycard" }, [summary, body]);
  card.open = state.open.has(record.id);
  card.addEventListener("toggle", () => {
    if (card.open) state.open.add(record.id);
    else state.open.delete(record.id);
  });
  return card;
}

/* ---------- the page ---------- */

function matches(record) {
  if (state.filter === "active" && record.archived_at) return false;
  if (state.filter === "archived" && !record.archived_at) return false;
  const query = state.query.trim().toLowerCase();
  if (!query) return true;
  return [record.name, record.state].filter(Boolean).some((value) =>
    value.toLowerCase().includes(query),
  );
}

export async function renderCities(root) {
  clear(root);

  const list = el("div", { class: "citylist" });
  const count = el("span", { class: "citytools__count" });

  function reload(saved, { check } = {}) {
    // The picker caches the city list; invalidate so it does not lag this view.
    city.invalidate();
    renderCities(root).then(() => {
      if (!check || !saved) return;
      const slot = root.querySelector(`[data-check-slot="${saved.id}"]`);
      if (slot) runCheck(saved.id, slot);
    });
  }

  const header = el("div", { class: "cityhead" }, [
    el("div", {}, [
      el("h1", { class: "cityhead__title", text: "Cities and their feeds" }),
      el("p", {
        class: "cityhead__blurb",
        text:
          "Manage cities and their news sources. Add new cities, configure feeds, " +
          "and monitor their health.",
      }),
    ]),
    el("span", { class: "citycard__gap" }),
    el("button", { type: "button", class: "btn btn--ink cityhead__add" }, [
      plusIcon(),
      el("span", { text: "Add city" }),
    ]),
  ]);
  header.querySelector(".cityhead__add").addEventListener("click", () => {
    openOnboardDialog({ onSaved: () => reload() });
  });

  const search = el("input", {
    class: "citysearch citysearch--icon",
    type: "search",
    placeholder: "Search cities (e.g. Jaipur, Nagpur)…",
    value: state.query,
    "aria-label": "Search cities",
  });
  // Filtering is client-side: there are a handful of cities and a five-feed cap,
  // so a server-side query would be machinery for a list that fits on a screen.
  search.addEventListener("input", () => {
    state.query = search.value;
    draw();
  });
  // The icon is a sibling, not a background-image: strokeIcon draws with
  // currentColor and a data-URI copy would freeze the colour.
  const searchBox = el("div", { class: "citysearch__box" }, [
    el("span", { class: "citysearch__icon", "aria-hidden": "true" }, [searchIcon()]),
    search,
  ]);

  const filterButtons = FILTERS.map(([value, label]) =>
    el("button", {
      type: "button",
      class: "chip",
      text: label,
      dataset: { filter: value },
      onclick: () => {
        state.filter = value;
        draw();
      },
    }),
  );
  const filters = el(
    "div",
    { class: "citytools__filters", role: "group", "aria-label": "Filter cities" },
    filterButtons,
  );

  const tools = el("div", { class: "citytools" }, [searchBox, filters, count]);
  root.append(header, tools, list);

  let records = [];

  function draw() {
    for (const button of filterButtons) {
      button.setAttribute("aria-pressed", String(button.dataset.filter === state.filter));
    }
    const shown = records.filter(matches);
    count.textContent = `${shown.length} ${shown.length === 1 ? "city" : "cities"}`;
    clear(list);
    if (shown.length === 0) {
      list.append(
        el("div", { class: "empty" }, [
          el("p", {
            class: "empty__head",
            text: records.length ? "Nothing matches." : "No cities yet.",
          }),
          el("p", {
            class: "empty__body",
            text: records.length
              ? "Try a different search, or switch the filter."
              : "Add the city you cover along with its news feeds, and stories " +
                "will start arriving within a few minutes.",
          }),
        ]),
      );
      return;
    }
    for (const record of shown) list.append(cityCard(record, { reload }));
  }

  try {
    // Archived cities come back too — the Archived filter is applied here, and
    // asking the server for one or the other would mean refetching on every
    // chip click.
    records = await apiFetch("/cities?include_archived=true");
    draw();
  } catch (error) {
    list.append(
      el("p", { class: "note note--bad", text: `Could not load cities. ${error.message}` }),
    );
  }
}
