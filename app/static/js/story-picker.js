/* "Add stories": the sheet the canvas opens to pull more stories into a deck.
 *
 * A picker, not a second Discover. Discover is for browsing — big cards, a
 * summary, a refresh button, a tray that survives navigation. This is for
 * choosing quickly against a deck that already exists, so the rows are compact
 * and the whole thing closes back onto the slide you were editing.
 *
 * It renders over the mounted editor rather than routing anywhere: <dialog> and
 * showModal() mean the canvas underneath keeps its active slide, its subscriber
 * and its unsaved textarea text, and cancelling is a close() with nothing to
 * restore.
 *
 * Every filter, query and badge here comes from feed.js. The list it replaced
 * had its own /stories call that ignored the category filter entirely — a
 * picker quietly disagreeing with the feed it claims to be showing. */

import { clear, el, openDialog, strokeIcon, timeAgo } from "./api.js";
import * as feed from "./feed.js";

/** One page, no paging. The filters are server-side and fifty rows is already a
 *  deep scroll — a "load more" here would be furniture. */
const LIMIT = 50;

function checkIcon() {
  return strokeIcon(["M4 10.5l4 4 8-9"], 13);
}

/** Secondary metadata, per add-story.md's hierarchy: the headline leads, this
 *  sits small and right-aligned. Gated on `engagement`, never on the score — an
 *  unclassified story is still listed and scores the floor, and printing that
 *  reads as a verdict. Same gate as the feed's own panel. */
function engagement(story) {
  if (!story.engagement) return null;
  const band = feed.engagementBand(story.score);
  return el("span", { class: "pickrow__score", title: feed.scoreExplainer(story) }, [
    el("span", {
      class: "pickrow__emoji",
      "aria-hidden": "true",
      text: feed.ENGAGEMENT_EMOJI[band - 1],
    }),
    el("span", { text: story.score.toFixed(1) }),
  ]);
}

/** One row. An <article role="button">, the same shape the feed card uses —
 *  the badges are flow content and a real <button> may not contain them, and
 *  aria-disabled rather than a disabled attribute for the same reason. */
function row(story, onToggle) {
  const node = el("article", {
    class: "pickrow",
    role: "button",
    tabindex: "0",
    "aria-pressed": "false",
    dataset: { storyId: story.id },
  });

  const check = el("span", { class: "pickrow__check", "aria-hidden": "true" });
  check.append(checkIcon());

  const top = el("div", { class: "pickrow__top" }, [
    feed.badges(story),
    // The surface where "have I used this?" actually gets asked.
    feed.usedBadge(story),
    el("span", { class: "pickrow__gap" }),
    engagement(story),
  ]);

  node.append(
    check,
    el("div", { class: "pickrow__body" }, [
      top,
      el("h3", { class: "pickrow__title", text: story.title }),
      el("p", {
        class: "pickrow__meta",
        text: `${story.source_name} · ${timeAgo(story.published_at)}`,
      }),
    ]),
  );

  const activate = () => onToggle(story.id);
  node.addEventListener("click", activate);
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      activate();
    }
  });
  return node;
}

/** Multi-select over the city's feed, capped at the room left in the deck.
 *  Resolves an array of story ids, or null on cancel.
 *
 *  `alreadyIn` is the set of story ids the deck already holds — re-adding one
 *  would duplicate a slide and a Selection row. */
export function openStoryPicker({ alreadyIn, room }) {
  return openDialog("dialog--sheet", (dialog, done) => {
    const chosen = new Set();
    // Its own filter state, seeded with Discover's sort so the picker opens in
    // the order the editor was last reading in. Deliberately not shared with
    // feed.js: narrowing the picker must not reorder the feed behind it.
    const filters = { categories: new Set(), sort: feed.currentSort() };

    const list = el("div", { class: "picklist" });
    const count = el("span", { class: "pickcount" });
    const footCount = el("span", { class: "pickcount" });
    const status = el("span", { class: "note" });

    const confirm = el("button", {
      type: "button",
      class: "btn btn--primary",
      text: "Add stories",
      onclick: () => done([...chosen]),
    });

    function syncRows() {
      const full = chosen.size >= room;
      for (const node of list.querySelectorAll(".pickrow")) {
        const picked = chosen.has(node.dataset.storyId);
        node.setAttribute("aria-pressed", String(picked));
        // Only the unpicked ones close: a full sheet must still let you drop
        // something to make room, which is the whole reason this is not a
        // blanket disable.
        node.setAttribute("aria-disabled", String(full && !picked));
      }
    }

    function syncCount() {
      const text = `${chosen.size} / ${room} selected`;
      count.textContent = text;
      footCount.textContent = text;
      confirm.disabled = chosen.size === 0;
      status.className = "note";
      status.textContent = chosen.size >= room ? "That is all that will fit." : "";
      syncRows();
    }

    function onToggle(id) {
      if (chosen.has(id)) chosen.delete(id);
      else if (chosen.size >= room) return;
      else chosen.add(id);
      syncCount();
    }

    function load() {
      clear(list);
      list.append(el("p", { class: "note", text: "Loading stories…" }));
      feed
        .fetchStories({ ...filters, limit: LIMIT })
        .then((stories) => {
          const available = stories.filter((story) => !alreadyIn.has(story.id));
          clear(list);
          if (available.length === 0) {
            list.append(
              el("p", {
                class: "note",
                text:
                  filters.categories.size
                    ? "Nothing matches those filters."
                    : "Nothing left in this city's window to add.",
              }),
            );
            return;
          }
          for (const story of available) list.append(row(story, onToggle));
          syncRows();
        })
        .catch((error) => {
          clear(list);
          list.append(
            el("p", { class: "note note--bad", text: `Could not load stories. ${error.message}` }),
          );
        });
    }

    const head = el("div", { class: "pickhead" }, [
      el("div", { class: "pickhead__text" }, [
        el("h2", { class: "pickhead__title", text: "Add stories" }),
        el("p", {
          class: "pickhead__sub",
          text: `Select up to ${room} ${room === 1 ? "story" : "stories"} to add to this carousel`,
        }),
      ]),
      count,
      el("button", {
        type: "button",
        class: "pickhead__close",
        text: "✕",
        "aria-label": "Close",
        onclick: () => done(null),
      }),
    ]);

    const controls = el("div", { class: "pickfilters" }, [
      feed.chiprow(
        feed.categoryIcon(),
        "Category",
        "Filter by category",
        feed.CATEGORIES,
        filters.categories,
        load,
      ),
      feed.sortToggle(filters, load),
    ]);

    const foot = el("div", { class: "pickfoot" }, [
      footCount,
      status,
      el("span", { class: "pickfoot__gap" }),
      // The one route out of here to Discover. Browsing is Discover's job and
      // this sheet's is not to grow into it.
      el("button", {
        type: "button",
        class: "btn btn--quiet btn--small",
        text: "Browse full Discover feed →",
        onclick: () => {
          window.location.hash = "discover";
          done(null);
        },
      }),
      el("button", {
        type: "button",
        class: "btn btn--quiet",
        text: "Cancel",
        onclick: () => done(null),
      }),
      confirm,
    ]);

    dialog.append(head, controls, list, foot);
    syncCount();
    load();
  });
}
