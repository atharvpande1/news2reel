/* The Creative Canvas: the deck on the left, the slide you are editing on the
 * right, and a zip at the end of it.
 *
 * Replaces the prompt step. Nothing is pasted anywhere now — POST /carousels
 * turns a selection into slides carrying each story's own headline, and they
 * render here. */

import {
  apiFetch,
  clear,
  confirmDialog,
  el,
  openDialog,
  safeUrl,
  strokeIcon,
  timeAgo,
} from "./api.js";
import * as feed from "./feed.js";
import * as slides from "./slides.js";
import { openStoryPicker } from "./story-picker.js";
import { HEIGHT, TEXT_MAX, WIDTH, drawSlide, renderToCanvas } from "./slide-canvas.js";

// JPEG quality for the export. High enough that type edges stay clean — text on
// flat white is exactly what JPEG handles worst — without doubling the payload.
const JPEG_QUALITY = 0.92;

let unsubscribe = null;
// Which slide the right pane is editing, as an index into the full deck.
let active = 0;

function backToFeed() {
  window.location.hash = "discover";
}

/** Reads off kind *and* story_id. A hand-written slide has kind "story" — it
 *  lays out like one and the canvas has no reason to know the difference — so
 *  the absent article is what distinguishes it. */
function kindLabel(slide) {
  if (slide.kind === "intro") return "Intro slide";
  if (slide.kind === "cta") return "Closing slide";
  return slide.story_id ? "Article slide" : "Custom slide";
}

/** A miniature of the slide. The canvas is the real 1080x1350 and CSS scales it
 *  down, so the thumbnail and the export are the same pixels — one renderer,
 *  nothing that can drift. See CLAUDE.md's invariants.
 *
 *  It spent a round as a DOM text card, which read better but showed nothing
 *  about layout: where the text sits, how much there is, whether a slide is
 *  overfull. That is what the strip is for; legibility lives in the inspector. */
function thumbnail(slide, index, onPick, onDelete) {
  const canvas = el("canvas", { class: "thumb__canvas" });
  canvas.width = WIDTH;
  canvas.height = HEIGHT;
  drawSlide(canvas, slide);

  const node = el("li", {
    class: `thumb${index === active ? " thumb--active" : ""}`,
    draggable: "true",
    dataset: { index: String(index) },
  });

  // el()'s children, not native append: a child that can be absent renders as
  // the literal word "null" through Node.append. See CLAUDE.md.
  node.append(
    canvas,
    el("span", { class: "thumb__n", text: String(index + 1) }),
    el("button", {
      type: "button",
      class: "thumb__drop",
      text: "✕",
      title: "Delete this slide",
      "aria-label": `Delete slide ${index + 1}`,
      onclick: (event) => {
        event.stopPropagation();
        onDelete(index);
      },
    }),
  );

  node.addEventListener("click", () => onPick(index));
  return node;
}

/** The trailing tile. Not draggable and carrying no index, so the drop handler
 *  ignores it — dropping a slide "onto" the add button has no meaning. */
function addTile(onAdd) {
  return el("li", { class: "thumb thumb--add" }, [
    el(
      "button",
      {
        type: "button",
        class: "thumb__add",
        title: "Add a story",
        "aria-label": "Add a story",
        onclick: onAdd,
      },
      [strokeIcon(["M10 4v12", "M4 10h12"], 20), el("span", { text: "Add story" })],
    ),
  ]);
}

/** HTML5 drag and drop over the filmstrip.
 *
 *  Wired on the list rather than per-thumbnail: the list survives a re-render
 *  and the thumbnails do not, so per-node listeners would have to be re-bound
 *  on every state change and one missed re-bind is a strip that silently stops
 *  accepting drops. */
function wireDragAndDrop(list) {
  let from = null;

  list.addEventListener("dragstart", (event) => {
    const item = event.target.closest(".thumb");
    if (!item || item.dataset.index === undefined) return;
    from = Number(item.dataset.index);
    // Firefox refuses to start a drag unless something is set.
    event.dataTransfer.setData("text/plain", String(from));
    event.dataTransfer.effectAllowed = "move";
  });

  list.addEventListener("dragover", (event) => {
    if (from === null) return;
    // Preventing the default is what marks this a valid drop target.
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  });

  list.addEventListener("drop", (event) => {
    const item = event.target.closest(".thumb");
    // The add tile is a .thumb too and carries no index. Without this guard it
    // reads as NaN, which slips past every range check downstream.
    if (from === null || !item || item.dataset.index === undefined) return;
    event.preventDefault();
    const to = Number(item.dataset.index);
    active = to;
    slides.move(from, to);
    from = null;
  });

  list.addEventListener("dragend", () => {
    from = null;
  });
}

function arrow(direction, disabled, onClick) {
  const path = direction === "prev" ? ["M12 5l-5 5 5 5"] : ["M8 5l5 5-5 5"];
  const button = el(
    "button",
    {
      type: "button",
      class: `stage__arrow stage__arrow--${direction}`,
      "aria-label": direction === "prev" ? "Previous slide" : "Next slide",
      onclick: onClick,
    },
    [strokeIcon(path, 18)],
  );
  button.disabled = disabled;
  return button;
}

/** "Punekar News" -> "PN". Initials, not a favicon: a masthead logo is a
 *  third-party request and safeUrl() cannot vouch for what comes back. */
function outletAvatar(name) {
  const initials = name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0].toUpperCase())
    .join("");
  return el("span", { class: "insp__avatar", "aria-hidden": "true", text: initials });
}

/** One labelled column in the signals row. `pill` is a .tag modifier when the
 *  value has one — reusing the feed's own colour tokens, so "crime" is the same
 *  pink on a card and on a slide. A second scale for the same enums is exactly
 *  the thing that drifts. */
function signal(label, value, pill) {
  return el("div", { class: "insp__signal" }, [
    el("span", { class: "insp__signal-label", text: label }),
    el("span", { class: pill ? `tag tag--${pill}` : "insp__signal-value", text: value }),
  ]);
}

/** The ⓘ beside the engagement score.
 *
 *  Generic copy on purpose, unlike the feed's per-card breakdown: that one names
 *  the dimensions that drove the number, which a slide does not carry, and
 *  putting the seven levels on the slide payload to reproduce one sentence is
 *  not worth a schema change. An unrated story carries no score at all, so this
 *  never appears beside a floor value pretending to be a verdict. */
function scoreInfo() {
  return el("span", {
    class: "insp__info",
    text: "i",
    "aria-hidden": "true",
    title:
      "Engagement score, 1-10: how well this story would work as a post. " +
      "Seven dimensions \u2014 emotional pull, how many people it touches, real " +
      "consequences, novelty, human interest, timeliness and visual potential. " +
      "Hover a card's rating in Discover for that story's own breakdown.",
  });
}

/** The source block: outlet, a way to the original, and the signals that got
 *  the story picked. Absent entirely on the brackets and on a slide written by
 *  hand, which is what `story_id` being null already tells us. */
function sourceBlock(slide) {
  if (!slide.story_id) return null;

  // Third-party feed data reaching an href — CLAUDE.md is explicit. rel guards
  // the opened page from getting a handle on window.opener.
  const href = safeUrl(slide.url);
  const open = href
    ? el(
        "a",
        {
          class: "insp__open",
          href,
          target: "_blank",
          rel: "noopener noreferrer",
        },
        [strokeIcon(["M7 4H4.5v11.5H16V13", "M11 4h5v5", "M16 4l-7 7"], 14), el("span", { text: "Open original article" })],
      )
    : null;

  return el("div", { class: "insp__block" }, [
    el("span", { class: "insp__label", text: "Source" }),
    el("div", { class: "insp__source" }, [
      slide.source_name && outletAvatar(slide.source_name),
      slide.source_name && el("span", { class: "insp__outlet", text: slide.source_name }),
      open,
    ]),
  ]);
}

/** The signals that got the story picked, as the mockup's three labelled
 *  columns. Its own block so the rule above it lands between source and
 *  signals rather than inside one of them. */
function signalRow(slide) {
  if (!slide.story_id) return null;
  // feed.engagementBand, not a second copy of the arithmetic: an inlined
  // duplicate does not follow a change to the scale.
  const band = typeof slide.score === "number" ? feed.engagementBand(slide.score) : null;

  return el("div", { class: "insp__signals" }, [
    slide.category && signal("Category", slide.category.replace(/_/g, " "), slide.category),
    band &&
      el("div", { class: "insp__signal" }, [
        el("span", { class: "insp__signal-label", text: "Engagement" }),
        el("span", { class: "insp__engagement" }, [
          el("span", { class: "insp__emoji", "aria-hidden": "true", text: feed.ENGAGEMENT_EMOJI[band - 1] }),
          el("span", { class: "insp__signal-value", text: slide.score.toFixed(1) }),
          scoreInfo(),
        ]),
      ]),
  ]);
}

/** The right-hand column: what this slide is, the field that edits it, and
 *  where it came from. */
function inspector(slide, index, redraw, total, position) {
  const count = el("span", { class: "insp__count" });
  const setCount = (value) => {
    count.textContent = `${value.length} / ${TEXT_MAX}`;
  };

  const field = el("textarea", {
    class: "insp__text",
    // A headline is one sentence and fits two rows. The brackets are built
    // from fixed multi-line templates instead — three lines for the intro's
    // headline/city/date — so two rows hands them a scrollbar on a field that
    // is never going to grow.
    rows: slide.kind === "story" ? "2" : "4",
    maxlength: String(TEXT_MAX),
    spellcheck: "true",
    "aria-label": "Slide text",
  });
  // .value, never innerHTML — slide text is a third-party feed headline, and
  // this is the one place it touches the DOM rather than a canvas. See
  // CLAUDE.md's "external input is hostile".
  field.value = slide.text;
  setCount(field.value);

  field.addEventListener("input", () => {
    // Redraw straight from the field so the canvases track every keystroke, and
    // persist through the store so a reload keeps the edit. setText does not
    // announce — a repaint per character is what used to eat the focus.
    redraw(field.value);
    slides.setText(index, field.value);
    setCount(field.value);
  });

  return el("div", { class: "insp" }, [
    el("div", { class: "insp__head" }, [
      el("span", { class: "insp__which", text: `Slide ${position} of ${total}` }),
      el("span", { class: "insp__chip", text: kindLabel(slide) }),
    ]),
    el("div", { class: "insp__block" }, [
      el("div", { class: "insp__label-row" }, [
        el("span", { class: "insp__label", text: "Slide text" }),
        slide.clipped === true &&
          el("span", {
            class: "insp__warn",
            text: "clipped headline",
            title:
              "This headline was longer than the slide's text box, so it was cut " +
              "at the last whole word. Worth a rewrite before it ships.",
          }),
        count,
      ]),
      field,
    ]),
    sourceBlock(slide),
    signalRow(slide),
  ]);
}

/** Two routes, not yes/no: pull another story from the feed, or write a slide
 *  by hand. Resolves "feed", "manual", or null. */
function chooseHowToAdd() {
  return openDialog("dialog--narrow", (dialog, done) => {
    const option = (title, body, value) =>
      el("button", { type: "button", class: "addopt", onclick: () => done(value) }, [
        el("span", { class: "addopt__title", text: title }),
        el("span", { class: "addopt__body", text: body }),
      ]);

    dialog.append(
      el("div", { class: "confirm" }, [
        el("h2", { class: "confirm__title", text: "Add a story" }),
        option(
          "Pick from the news feed",
          "Choose from what is still in this city's window. Each one arrives as its own headline.",
          "feed",
        ),
        option(
          "Write one myself",
          "A blank slide with no article behind it. You supply the text.",
          "manual",
        ),
        el("div", { class: "confirm__actions" }, [
          el("button", {
            type: "button",
            class: "btn btn--quiet",
            text: "Cancel",
            onclick: () => done(null),
          }),
        ]),
      ]),
    );
  });
}

/** Confirms, then deletes for real — there is no undo to fall back on, which is
 *  exactly why it asks. Quotes the slide so the dialog names what is going. */
async function deleteSlide(index) {
  const slide = slides.all()[index];
  if (!slide) return;
  const quoted = slide.text.trim().replace(/\s+/g, " ").slice(0, 80);
  const ok = await confirmDialog({
    title: `Delete slide ${index + 1}?`,
    body: quoted ? `“${quoted}${slide.text.length > 80 ? "…" : ""}” will be removed from the carousel.` : "This slide will be removed from the carousel.",
    confirmLabel: "Delete slide",
  });
  if (!ok) return;
  slides.remove(index);
  // Keep the selection inside the deck it just shrank.
  if (active >= slides.size()) active = Math.max(0, slides.size() - 1);
}

/** The "Add story" tile's whole job. Returns the index to select, or null. */
async function addStory(status) {
  const room = slides.room();
  if (room === 0) {
    status.className = "note note--bad";
    status.textContent = `A carousel holds ${slides.MAX_SLIDES} slides.`;
    return null;
  }

  const how = await chooseHowToAdd();
  if (how === null) return null;
  if (how === "manual") return slides.add();

  const alreadyIn = new Set(slides.all().map((slide) => slide.story_id).filter(Boolean));
  const picked = await openStoryPicker({ alreadyIn, room });
  if (!picked || picked.length === 0) return null;

  status.className = "note";
  status.textContent = "";
  try {
    // The server inserts before the CTA and hands back the whole deck — one
    // place that knows where a story slide goes, rather than the client
    // splicing a fragment into its own copy and the two drifting.
    const result = await apiFetch(`/carousels/${slides.currentId()}/stories`, {
      method: "POST",
      body: { story_ids: picked, sort: feed.currentSort() },
    });
    slides.load(result.id, result.slides);
    status.textContent = "";
    // Select the first one that was just added. Found by id rather than by
    // arithmetic on where the CTA moved to — the server decides the order, and
    // this asks it rather than re-deriving it.
    const added = result.slides.findIndex((slide) => picked.includes(slide.story_id));
    return added === -1 ? null : added;
  } catch (error) {
    status.className = "note note--bad";
    status.textContent = `Could not add. ${error.message}`;
    return null;
  }
}

async function downloadZip(status) {
  const keep = slides.all();
  if (keep.length === 0) {
    status.className = "note note--bad";
    status.textContent = "There are no slides to export.";
    return;
  }

  status.className = "note";
  status.textContent = "Rendering…";
  const images = keep.map((slide) => renderToCanvas(slide).toDataURL("image/jpeg", JPEG_QUALITY));

  try {
    const blob = await apiFetch(`/carousels/${slides.currentId()}/zip`, {
      method: "POST",
      body: { images },
      blob: true,
    });
    const url = URL.createObjectURL(blob);
    const link = el("a", { href: url, download: "carousel.zip" });
    document.body.append(link);
    link.click();
    link.remove();
    // Revoking immediately can race the download in some browsers; a tick is
    // enough and the object is small.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.className = "note note--ok";
    status.textContent = `Downloaded ${keep.length} slides.`;
  } catch (error) {
    status.className = "note note--bad";
    status.textContent = `Could not build the zip. ${error.message}`;
  }
}

export async function renderStudio(root) {
  if (unsubscribe) unsubscribe();
  clear(root);

  const status = el("span", { class: "note" });
  const body = el("div", { class: "studio" });
  const back = el(
    "button",
    { type: "button", class: "studio__back", onclick: backToFeed },
    [strokeIcon(["M16 10H4", "M9 5l-5 5 5 5"], 16), el("span", { text: "Back to stories" })],
  );
  const download = el(
    "button",
    {
      type: "button",
      class: "btn btn--primary",
      onclick: () => downloadZip(status),
    },
    [
      strokeIcon(["M10 3v9", "M6 9l4 4 4-4", "M4 16h12"], 16),
      el("span", { text: "Download zip" }),
    ],
  );

  // Re-rendered on every deck change, so the count and the saved time stay
  // true without a second subscriber of their own.
  const meta = el("span", { class: "studio__meta" });
  const paintMeta = () => {
    const n = slides.size();
    const at = slides.lastSavedAt();
    clear(meta);
    meta.append(
      el("span", { text: `${n} ${n === 1 ? "slide" : "slides"}` }),
      at ? el("span", { text: ` · Saved ${timeAgo(new Date(at).toISOString())}` }) : "",
    );
  };

  root.append(
    el("div", { class: "studio__head" }, [
      back,
      el("span", { class: "studio__rule", "aria-hidden": "true" }),
      el("div", { class: "studio__title" }, [
        el("span", { class: "studio__name", text: "Create carousel" }),
        meta,
      ]),
      el(
        "span",
        {
          class: "studio__saved",
          // The nuance belongs in the tooltip: sessionStorage is this tab and
          // this session, and an unqualified "Auto-saved" reads as "safe on a
          // server" — which someone would find out is untrue at the worst
          // possible moment.
          title: "Saved in this browser tab. Closing it discards the deck.",
        },
        [el("span", { class: "studio__dot", "aria-hidden": "true" }), el("span", { text: "Auto-saved" })],
      ),
      el("span", { class: "studio__spacer" }),
      status,
      download,
    ]),
    body,
  );

  function paint() {
    clear(body);
    paintMeta();
    const deck = slides.all();
    if (deck.length === 0) {
      // Deleting the last slide would otherwise leave a header over nothing.
      body.append(
        el("div", { class: "empty" }, [
          el("p", { class: "empty__head", text: "No slides left." }),
          el("p", {
            class: "empty__body",
            text: "Every slide has been deleted. Go back and pick stories to start a new carousel.",
          }),
          el("p", { class: "centered" }, [
            el("button", {
              type: "button",
              class: "btn btn--ink",
              text: "Back to stories",
              onclick: backToFeed,
            }),
          ]),
        ]),
      );
      return;
    }
    if (active >= deck.length) active = deck.length - 1;

    const slide = deck[active];
    const preview = el("canvas", { class: "stage__canvas" });
    preview.width = WIDTH;
    preview.height = HEIGHT;
    drawSlide(preview, slide);

    const show = (index) => {
      active = index;
      paint();
    };

    const list = el("ul", { class: "filmstrip" });
    deck.forEach((item, index) => list.append(thumbnail(item, index, show, deleteSlide)));
    wireDragAndDrop(list);

    // Both canvases, from one edited copy: the thumbnail is the preview scaled
    // (see CLAUDE.md), so letting it lag a keystroke behind would be the two
    // renderers disagreeing that invariant exists to prevent.
    const thumbCanvas = list.querySelector(".thumb--active .thumb__canvas");
    const redraw = (text) => {
      const edited = { ...slide, text };
      drawSlide(preview, edited);
      if (thumbCanvas) drawSlide(thumbCanvas, edited);
    };
    list.append(
      addTile(async () => {
        const index = await addStory(status);
        // null means cancelled, capped, or failed — addStory has already said
        // which, so there is nothing to select and nothing more to report.
        if (index !== null) show(index);
      }),
    );

    // Preview on top with its arrows, then the editor, then the deck. The
    // stage grows into whatever height is left and everything else keeps its
    // own, so the whole canvas fits one screen.
    body.append(
      el("div", { class: "stage" }, [
        // The arrows float in the preview's own gutters rather than in the
        // inspector: stepping through the deck then never moves the eye away
        // from the thing that changed.
        el("div", { class: "stage__slide" }, [
          arrow("prev", active === 0, () => show(active - 1)),
          preview,
          arrow("next", active === deck.length - 1, () => show(active + 1)),
        ]),
        inspector(slide, active, redraw, deck.length, active + 1),
      ]),
      list,
    );
  }

  unsubscribe = slides.subscribe(paint);

  // This view never builds a carousel, it only edits the one that is loaded.
  // It used to build whenever it found no deck in session storage — a proxy
  // for "the editor just picked stories" that held only while a deck lived for
  // one sitting. Now that every deck is saved, that condition is false on the
  // second visit onwards, and Next reopened the previous carousel instead of
  // making a new one. Building belongs to the click that means it: Next on
  // Discover, or a card in the Gallery. See feed.js's buildCarousel.
  const note = slides.takeNotice();
  if (note) {
    status.className = "note note--bad";
    status.textContent = note;
  }

  paint();
}
