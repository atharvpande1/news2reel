/* The Gallery: every carousel, most recently worked on first.
 *
 * An archive, not a feed — which is the one place this app is not scoped to a
 * single city. The work you are looking for is as likely to be in the city you
 * covered last week, so this lists all of them and carries its own city filter.
 * The masthead picker belongs to Discover and deliberately does not reach here:
 * a switcher that silently emptied an archive would read as lost work.
 *
 * Fetched once, unfiltered, and narrowed in the browser. That is what makes the
 * chips' counts — "All (12) / Draft (4) / Downloaded (8)" — free: those totals
 * have to hold whichever filter is active, so the whole list is needed anyway.
 * See services/gallery.py for the matching note on the server side. */

import { apiFetch, clear, confirmDialog, el, strokeIcon, timeAgo } from "./api.js";
import * as slides from "./slides.js";

/* Must match settings.carousel_timezone. Hardcoded because nothing serves the
 * server's settings to the browser — selection.js and slides.js mirror their
 * caps the same way. */
const ZONE = "Asia/Kolkata";

/* en-US for the parts, our own order for the layout. en-GB would put the day
 * first for free but abbreviates September as "Sept", the one month that is
 * four letters — which shifts the line in that card and no other. */
const STAMP = new Intl.DateTimeFormat("en-US", {
  timeZone: ZONE,
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
  hour12: true,
});

/** The card's timestamp, and the order the grid is in. A shown date that is not
 *  the sort key looks like a sorting bug, so these are the same value — the
 *  server orders on exactly this coalesce. */
const workedOn = (row) => row.last_edited_at ?? row.created_at;

/** "19 Sep 2026 · 1:56 PM". */
function stamp(row) {
  const part = Object.fromEntries(
    STAMP.formatToParts(new Date(workedOn(row))).map(({ type, value }) => [type, value]),
  );
  return `${part.day} ${part.month} ${part.year} · ${part.hour}:${part.minute} ${part.dayPeriod}`;
}

const state = { filter: null, cityId: null };
let rows = [];

/** One file-type glyph for every card: two stacked slides, the shape the
 *  format is recognised by everywhere else.
 *
 *  It replaced a real rendered first slide. That was the same drawSlide the
 *  filmstrip uses, which is right *inside* a deck where the job is telling
 *  slides apart — but here every deck opens on the same intro template, so
 *  forty cards ran the canvas renderer to draw forty near-identical miniatures.
 *  The city and the timestamp are what actually identify a row. */
function carouselIcon() {
  const tile = el("span", { class: "gcard__icon", "aria-hidden": "true" });
  tile.append(
    strokeIcon(
      [
        // The slide in front.
        "M9 4.5h5A1.5 1.5 0 0115.5 6v8a1.5 1.5 0 01-1.5 1.5H9A1.5 1.5 0 017.5 14V6A1.5 1.5 0 019 4.5z",
        // And the one behind it, peeking out on the left.
        "M5.5 12.8A1.5 1.5 0 014 11.3V8.7a1.5 1.5 0 011.5-1.5",
      ],
      22,
    ),
  );
  return tile;
}

/** "edited 2h ago", and only when it is actually true. Both timestamps exist
 *  precisely so this can be said — the state stays "downloaded", because it did
 *  ship; this is the caveat on top. */
function staleNote(row) {
  if (!row.last_downloaded_at || !row.last_edited_at) return null;
  if (Date.parse(row.last_edited_at) <= Date.parse(row.last_downloaded_at)) return null;
  return el("span", {
    class: "gcard__stale",
    text: `edited ${timeAgo(row.last_edited_at)}`,
    title: "This carousel has been edited since the zip was downloaded.",
  });
}

function trashIcon() {
  return strokeIcon(["M4 6h12", "M7.5 6V4.5h5V6", "M5.5 6l.8 10h7.4l.8-10", "M8.5 9v4", "M11.5 9v4"], 16);
}

function card(row, onOpen, onDelete) {
  const node = el("article", {
    class: "gcard",
    role: "button",
    tabindex: "0",
    dataset: { state: row.state },
  });

  const open = () => onOpen(row);
  node.addEventListener("click", open);
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      open();
    }
  });

  node.append(
    el("div", { class: "gcard__head" }, [
      carouselIcon(),
      el("div", { class: "gcard__ident" }, [
        el("h3", { class: "gcard__city", text: row.city_name ?? "Untitled" }),
        el("p", { class: "gcard__when", text: stamp(row) }),
        el("p", {
          class: "gcard__count",
          text: `${row.slide_count} ${row.slide_count === 1 ? "slide" : "slides"}`,
        }),
      ]),
    ]),
    el("div", { class: "gcard__foot" }, [
      el("span", { class: `gtag gtag--${row.state}`, text: row.state }),
      staleNote(row),
      el("span", { class: "gcard__gap" }),
      el(
        "button",
        {
          type: "button",
          class: "gcard__drop",
          title: "Delete this carousel",
          "aria-label": `Delete the ${row.city_name ?? "untitled"} carousel from ${stamp(row)}`,
          onclick: (event) => {
            event.stopPropagation();
            onDelete(row);
          },
        },
        [trashIcon()],
      ),
    ]),
  );
  return node;
}

/** The state pills, with counts over the whole archive.
 *
 *  Single-select and counted, so not feed.js's chiprow — that one is
 *  multi-select over a Set and carries no counts. Same .chip styling, so they
 *  still read as the filters they are. */
function statePills(onChange) {
  const group = el("div", { class: "chiprow", role: "group", "aria-label": "Filter by state" });
  const counts = {
    null: rows.length,
    draft: rows.filter((r) => r.state === "draft").length,
    downloaded: rows.filter((r) => r.state === "downloaded").length,
  };
  const pill = (label, value) =>
    el("button", {
      type: "button",
      class: "chip",
      text: `${label} (${counts[value]})`,
      "aria-pressed": String(state.filter === value),
      onclick: () => {
        state.filter = value;
        onChange();
      },
    });
  group.append(pill("All", null), pill("Draft", "draft"), pill("Downloaded", "downloaded"));
  return group;
}

/** A native <select>, deliberately unlike the pills beside it: the state filter
 *  and the city filter are different axes, and a second chip row would read as
 *  more of the same. It also stays usable at twenty cities, where chips wrap. */
function cityFilter(onChange) {
  const seen = new Map();
  for (const row of rows) {
    if (row.city_id !== null) seen.set(row.city_id, row.city_name);
  }

  const select = el("select", { class: "input gfilters__city", "aria-label": "Filter by city" });
  select.append(el("option", { value: "", text: "All cities" }));
  for (const [id, name] of [...seen].sort((a, b) => String(a[1]).localeCompare(String(b[1])))) {
    const option = el("option", { value: String(id), text: name ?? "Untitled" });
    if (state.cityId === id) option.selected = true;
    select.append(option);
  }
  select.addEventListener("change", () => {
    state.cityId = select.value === "" ? null : Number(select.value);
    onChange();
  });

  return el("label", { class: "field" }, [el("span", { text: "City" }), select]);
}

function emptyState(filtered) {
  return el("div", { class: "empty" }, [
    el("p", {
      class: "empty__head",
      text: filtered ? "Nothing matches those filters." : "No carousels yet.",
    }),
    el("p", {
      class: "empty__body",
      text: filtered
        ? "Everything you have made is under All, in every city."
        : "Pick stories on Discover and build one — it is saved here the moment it exists.",
    }),
    el("p", { class: "centered" }, [
      el("button", {
        type: "button",
        class: "btn btn--ink",
        text: "Go to Discover",
        onclick: toDiscover,
      }),
    ]),
  ]);
}

function toDiscover() {
  window.location.hash = "discover";
}

export async function renderGallery(root) {
  clear(root);

  const filters = el("div", { class: "gfilters" });
  const grid = el("div", { class: "gallery" });
  // Its own class, not .centered: that one pads 24px top and bottom, which an
  // empty status bar was spending as dead space between the filters and the
  // grid. `.gallery__status:empty` collapses it instead.
  const status = el("div", { class: "gallery__status" });

  // No page title, and no header row either: the nav tab and the masthead
  // already name this page, and a line holding one right-aligned button was
  // the empty space pushing the first row of cards under the fold. Create
  // lives on the filter row, which had a gap in the middle of it anyway.
  root.append(filters, status, grid);

  async function openCarousel(row) {
    status.replaceChildren(el("span", { class: "note", text: "Opening…" }));
    try {
      const result = await apiFetch(`/carousels/${row.id}`);
      // Loaded here rather than by the canvas, so the deck and the id it saves
      // to arrive together — the canvas only ever edits what it is handed.
      slides.load(result.id, result.slides);
      window.location.hash = "studio";
    } catch (error) {
      status.replaceChildren(
        el("p", { class: "note note--bad", text: `Could not open it. ${error.message}` }),
      );
    }
  }

  async function deleteCarousel(row) {
    const ok = await confirmDialog({
      title: "Delete this carousel?",
      body: `${row.slide_count} ${row.slide_count === 1 ? "slide" : "slides"} will be removed. This cannot be undone.`,
      confirmLabel: "Delete carousel",
    });
    if (!ok) return;
    try {
      await apiFetch(`/carousels/${row.id}`, { method: "DELETE" });
      // The open deck was the one just deleted — drop it rather than leaving
      // the canvas autosaving into a row that is gone.
      if (slides.currentId() === row.id) slides.clear();
      await load();
    } catch (error) {
      status.replaceChildren(
        el("p", { class: "note note--bad", text: `Could not delete it. ${error.message}` }),
      );
    }
  }

  /* Repaints from `rows`, which is already in hand — no request. The counts are
   * always over the whole archive, never over what the current filter left. */
  function createButton() {
    return el(
      "button",
      {
        type: "button",
        class: "btn btn--primary gfilters__create",
        // Building starts with picking stories, and that is Discover's job —
        // see feed.js's buildCarousel, the one place a carousel is born.
        onclick: toDiscover,
      },
      [strokeIcon(["M10 4v12", "M4 10h12"], 16), el("span", { text: "Create carousel" })],
    );
  }

  function paint() {
    filters.replaceChildren(
      statePills(paint),
      el("span", { class: "gfilters__gap" }),
      cityFilter(paint),
      createButton(),
    );
    clear(grid);

    const shown = rows.filter(
      (row) =>
        (state.filter === null || row.state === state.filter) &&
        (state.cityId === null || row.city_id === state.cityId),
    );
    if (shown.length === 0) {
      grid.append(emptyState(rows.length > 0));
      return;
    }
    for (const row of shown) grid.append(card(row, openCarousel, deleteCarousel));
  }

  async function load() {
    status.replaceChildren(el("span", { class: "note", text: "Loading…" }));
    try {
      rows = await apiFetch("/carousels");
      clear(status);
      paint();
    } catch (error) {
      rows = [];
      clear(grid);
      status.replaceChildren(
        el("p", { class: "note note--bad", text: `Could not load the gallery. ${error.message}` }),
      );
    }
  }

  await load();
}
