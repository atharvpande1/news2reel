/* Shared layer: HTTP, URL safety, DOM construction, time formatting.
 *
 * Two rules the rest of the UI depends on, both from CLAUDE.md's "External
 * input is hostile": every URL that reaches an href/src goes through
 * safeUrl(), and every piece of API text reaches the DOM as textContent.
 * el() exists so no module has a reason to build markup from strings. */

const API_BASE = "/api/v1";

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

/** FastAPI returns validation errors as {detail: [{loc, msg}, ...]}, which
 *  stringifies to "[object Object]" if handed straight to the user. */
function describeError(status, body) {
  const detail = body && body.detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        const field = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
        return field ? `${field}: ${d.msg}` : d.msg;
      })
      .join("; ");
  }
  if (typeof detail === "string") return detail;
  return `request failed (${status})`;
}

/* Session refresh. The access cookie lives 15 minutes; when a request comes
 * back 401 the refresh cookie buys a new pair and the request is retried once.
 * Single-flight: a view that fires six requests at once gets six 401s, and
 * they must share one refresh rather than race six. A failed refresh means the
 * session is over — main.js hears `feedcast:logged-out` and shows the login. */
let refreshing = null;

function refreshSession() {
  refreshing ??= fetch(`${API_BASE}/auth/refresh`, { method: "POST", cache: "no-store" })
    .then((response) => response.ok)
    .catch(() => false)
    .finally(() => {
      refreshing = null;
    });
  return refreshing;
}

/** `blob: true` returns the response body as a Blob instead of parsed JSON —
 *  the zip download needs binary back. Only the success path changes: a failed
 *  request still comes back as JSON and goes through describeError, because the
 *  zip endpoint reports its caps as ordinary 4xx detail. */
export async function apiFetch(path, options = {}) {
  const { blob = false, ...rest } = options;
  // no-store: the API sets no cache headers, so a browser is free to serve a
  // heuristically-cached response. On a feed that changes every scheduler tick
  // that shows up as stale stories with no indication anything is wrong.
  const init = { cache: "no-store", ...rest, headers: { ...(rest.headers || {}) } };
  if (init.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }

  let response = await fetch(`${API_BASE}${path}`, init);
  // Never for /auth/*: a failed login is an answer, not an expired session,
  // and refreshing on a failed refresh would loop.
  if (response.status === 401 && !path.startsWith("/auth/")) {
    if (await refreshSession()) response = await fetch(`${API_BASE}${path}`, init);
    if (response.status === 401) window.dispatchEvent(new Event("feedcast:logged-out"));
  }
  if (blob && response.ok) return response.blob();
  const text = await response.text();
  // Not every response is JSON. An unhandled server error comes back as plain
  // "Internal Server Error", and parsing it unguarded replaced the status with
  // a JSON syntax error — which is exactly the wrong thing to show someone
  // trying to work out what broke.
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    if (!response.ok) throw new ApiError(response.status, `request failed (${response.status})`);
    throw new ApiError(response.status, "the server sent a response that could not be read");
  }
  if (!response.ok) throw new ApiError(response.status, describeError(response.status, body));
  return body;
}

/** Returns the URL only if it is http(s). Feed-supplied article and image
 *  URLs are third-party data — never let one reach an href/src unchecked. */
export function safeUrl(value) {
  if (!value || typeof value !== "string") return null;
  try {
    const parsed = new URL(value, window.location.origin);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

/** Minimal element builder. `text` is assigned via textContent, so API data
 *  can never be parsed as markup. */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child);
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
}

export function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "";
  const seconds = Math.max(0, (Date.now() - then.getTime()) / 1000);
  if (seconds < 60) return "just now";
  // Largest unit that fits, so 90 minutes reads "1h ago" not "90m ago".
  for (const [suffix, size] of [
    ["d", 86400],
    ["h", 3600],
    ["m", 60],
  ]) {
    const value = Math.floor(seconds / size);
    if (value >= 1) return `${value}${suffix} ago`;
  }
  return "just now";
}

/** A source is "verified" when it is actually pulling cleanly. Anything else
 *  — disabled, archived, erroring, backing off — loses the tick, so the badge
 *  carries real information rather than always being true. */
export function isHealthy(source) {
  return Boolean(
    source &&
      source.enabled &&
      !source.archived_at &&
      !source.last_error &&
      source.consecutive_failures === 0,
  );
}

/** Copy text to the clipboard, reporting whether it worked.
 *
 *  navigator.clipboard is undefined outside a secure context. http://127.0.0.1
 *  counts as one; http://<lan-ip>:8000 — how anyone else in the newsroom will
 *  open this — does not. Without the fallback, Copy silently does nothing on
 *  exactly the machines that are not the one running the server. */
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through — a rejected permission is still worth retrying the old way.
  }
  const staging = el("textarea", { class: "sr-only", "aria-hidden": "true" });
  staging.value = text;
  document.body.append(staging);
  staging.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    staging.remove();
  }
}

/** A yes/no dialog, resolving true only on confirm.
 *
 *  Lives here with el() and copyText() because it is shared furniture, not
 *  feed-specific. Built on <dialog> and showModal() like the city picker and
 *  the onboarding form, so focus trapping, the backdrop and Escape come from
 *  the platform rather than from us.
 *
 *  `cancel` resolves false as well as the Cancel button: Escape and a backdrop
 *  dismissal fire that event and nothing else, so without it the promise would
 *  hang and the caller would sit waiting forever on a dialog that is gone. */
export function confirmDialog({ title, body, confirmLabel = "Continue" }) {
  return new Promise((resolve) => {
    const dialog = el("dialog", { class: "dialog--narrow" });
    let answer = false;

    const finish = (value) => {
      answer = value;
      dialog.close();
    };

    dialog.append(
      el("div", { class: "confirm" }, [
        el("h2", { class: "confirm__title", text: title }),
        body && el("p", { class: "confirm__body", text: body }),
        el("div", { class: "confirm__actions" }, [
          el("button", {
            type: "button",
            class: "btn btn--quiet",
            text: "Cancel",
            onclick: () => finish(false),
          }),
          el("button", {
            type: "button",
            class: "btn btn--primary",
            text: confirmLabel,
            onclick: () => finish(true),
          }),
        ]),
      ]),
    );

    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(answer);
    });

    document.body.append(dialog);
    dialog.showModal();
  });
}

/** A <dialog> built, shown, and removed on close. `fill` gets the dialog so it
 *  can close it; whatever it hands back through `done` is what the promise
 *  resolves to — the generic shape confirmDialog() above is one instance of.
 *
 *  Lives here rather than in studio.js because the story picker needs it too,
 *  and studio.js imports the picker — the other direction would be a cycle. */
export function openDialog(className, fill) {
  return new Promise((resolve) => {
    const dialog = el("dialog", { class: className });
    let answer = null;
    // Resolve from `close`, not from the buttons: Escape and a backdrop
    // dismissal fire cancel/close and nothing else, so resolving anywhere else
    // leaves the promise hanging on a dialog that is already gone.
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(answer);
    });
    fill(dialog, (value) => {
      answer = value;
      dialog.close();
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}

/** One stroke-style icon builder, shared by every small glyph in the UI — the
 *  feed's filter-row and sort-chip icons, and the canvas's arrows and download
 *  mark. One implementation, so a new icon is one array of path data rather
 *  than a fresh thirteen-line SVG block. */
export function strokeIcon(paths, size = 14) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("aria-hidden", "true");
  for (const d of paths) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}
