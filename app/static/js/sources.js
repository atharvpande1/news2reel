/* One feed: its form fields, its health, and the live check.
 *
 * No renderer of its own — sources are listed inside their city, so cities.js
 * owns the view and imports these pieces. Three things here are load-bearing
 * and marked as such below: the precedence order in healthState, reading
 * checkboxes off the form element rather than FormData, and branching on
 * result.ok rather than response.ok. */

import { apiFetch, clear, el, isHealthy } from "./api.js";

export const FIELDS = [
  { name: "name", label: "Name", placeholder: "TOI Nagpur", required: true },
  {
    name: "feed_url",
    label: "Feed URL",
    placeholder: "https://example.com/rss.xml",
    required: true,
  },
  { name: "publisher_group", label: "Publisher group", placeholder: "toi", required: true },
  { name: "language", label: "Language", placeholder: "en", required: true },
  {
    name: "is_local_outlet",
    label: "Local outlet — treat every article as local, skip the URL city check",
    type: "checkbox",
  },
  {
    name: "fetch_interval_minutes",
    label: "Check every (minutes)",
    type: "number",
    // Mirrors SourceCreate's Field(ge=1, le=10080) so the browser rejects the
    // same values the API would.
    attrs: { min: "1", max: "10080" },
    value: "30",
    required: true,
  },
];

/** A feed's health as data: one of "archived", "paused", "failing",
 *  "unchecked", "ok", "stale". Separate from the rendering below so a caller
 *  can *count* healthy feeds without re-deriving the rule — the city header's
 *  "2 of 5 feeds" and the row's own label have to agree by construction, not
 *  because both were written carefully.
 *
 *  The order is the whole logic and is load-bearing. "unchecked" is tested
 *  before isHealthy, not after: a source nobody has fetched yet has no error
 *  and no failures either, so isHealthy calls it clean, and saying "pulling
 *  cleanly" about a feed nobody has tried is just wrong. */
export function healthState(source) {
  if (source.archived_at) return "archived";
  if (!source.enabled) return "paused";
  if (source.consecutive_failures > 0) return "failing";
  if (!source.last_fetched_at) return "unchecked";
  return isHealthy(source) ? "ok" : "stale";
}

const HEALTH_LOOK = {
  archived: ["warn", () => "Archived"],
  paused: ["warn", () => "Paused"],
  failing: ["bad", (s) => `Failing, ${s.consecutive_failures} in a row`],
  unchecked: ["warn", () => "Not checked yet"],
  ok: ["ok", () => "Pulling cleanly"],
  stale: ["warn", () => "Needs a look"],
};

export function health(source) {
  const [tone, label] = HEALTH_LOOK[healthState(source)];
  return el("span", { class: `health health--${tone}`, text: label(source) });
}

/** The most recent evidence that anything looked at this feed.
 *
 *  Not just `last_fetched_at`: that is the scheduler's stamp, and
 *  POST /sources/{id}/check deliberately leaves it alone so a manual check does
 *  not push back the next scheduled poll. Reading it alone would leave "Last
 *  checked" frozen right after someone clicked Check now, which looks broken. */
export function lastCheckedAt(source) {
  const stamps = [source.last_fetched_at, source.last_success_at, source.last_error_at]
    .filter(Boolean)
    .map((iso) => new Date(iso))
    .filter((date) => !Number.isNaN(date.getTime()));
  if (stamps.length === 0) return null;
  return new Date(Math.max(...stamps)).toISOString();
}

export function field(spec, initial) {
  if (spec.type === "checkbox") {
    const box = el("input", { name: spec.name, type: "checkbox" });
    box.checked = Boolean(initial);
    return el("label", { class: "form__check" }, [box, el("span", { text: spec.label })]);
  }
  const input = el("input", {
    name: spec.name,
    type: spec.type || "text",
    placeholder: spec.placeholder || "",
    value: initial !== undefined && initial !== null ? String(initial) : spec.value || "",
    required: spec.required ? "required" : null,
    class: "form__input",
    ...(spec.attrs || {}),
  });
  return el("label", { class: "block" }, [
    el("span", { class: "form__label", text: spec.label }),
    input,
  ]);
}

export function readForm(form, specs = FIELDS, prefix = "") {
  const data = new FormData(form);
  const body = {};
  for (const spec of specs) {
    const key = `${prefix}${spec.name}`;
    if (spec.type === "checkbox") {
      // An unchecked box submits nothing at all, so FormData can't tell
      // "unchecked" from "absent" — read the element instead.
      body[spec.name] = Boolean(form.elements[key]?.checked);
      continue;
    }
    const raw = (data.get(key) || "").toString().trim();
    if (!raw) continue;
    body[spec.name] = spec.type === "number" ? Number(raw) : raw;
  }
  return body;
}

/** POST /sources/{id}/check answers 200 even for a dead feed — the outcome is
 *  in the body. Reading response.ok here would call every failure a success. */
export async function runCheck(sourceId, into) {
  into.replaceChildren(el("span", { class: "note", text: "Checking…" }));
  try {
    const result = await apiFetch(`/sources/${sourceId}/check`, { method: "POST" });
    into.replaceChildren(
      result.ok
        ? el("span", {
            class: "note note--ok",
            text: `Reached it — ${result.entry_count ?? 0} entries`,
          })
        : el("span", { class: "note note--bad", text: result.error || "The check failed" }),
    );
  } catch (error) {
    into.replaceChildren(el("span", { class: "note note--bad", text: error.message }));
  }
}

export function openSourceDialog({ title, source, submit, onSaved }) {
  const dialog = el("dialog");
  const error = el("p", { class: "note note--bad" });
  const form = el("form", { class: "form" }, [
    el("h2", { class: "panel__title", text: title }),
    ...FIELDS.map((spec) => field(spec, source ? source[spec.name] : undefined)),
    error,
    el("div", { class: "form__actions" }, [
      el("button", {
        type: "button",
        class: "btn btn--quiet",
        text: "Cancel",
        onclick: () => dialog.close(),
      }),
      el("button", { type: "submit", class: "btn btn--ink", text: source ? "Save" : "Add source" }),
    ]),
  ]);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clear(error);
    const body = readForm(form);
    try {
      // Create goes through the city — there is no standalone POST /sources —
      // so the caller supplies it and only the edit path is known here.
      const saved = source
        ? await apiFetch(`/sources/${source.id}`, { method: "PATCH", body })
        : await submit(body);
      dialog.close();
      // The API can only check a source that exists, so a new one is verified
      // immediately after creation rather than before.
      onSaved(saved, { check: !source });
    } catch (err) {
      error.textContent = err.message;
    }
  });

  // Removed on close, not on submit: Cancel and Escape both close without
  // submitting, and the old code left a detached <dialog> in the body each time.
  dialog.addEventListener("close", () => dialog.remove());

  dialog.append(form);
  document.body.append(dialog);
  dialog.showModal();
}
