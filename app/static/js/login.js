/* #login — the one route that renders without a session.
 *
 * A split page: the pitch on the left, the form on the right. No signup and no
 * password reset — accounts are inserted by hand (CLAUDE.md, "Auth") — so
 * there is nothing to link to. The hero copy describes what the tool does
 * today and nothing it does not: it finds and ranks stories, builds carousels,
 * and keeps them in the Gallery. It does not publish; a person still posts.
 *
 * The cookies a login earns are httpOnly, so nothing here ever sees a token;
 * success is simply the server saying yes. */

import { apiFetch, clear, el, strokeIcon } from "./api.js";

const ICONS = {
  // A page of text: the day's stories.
  feed: [
    "M6 3h8a1.5 1.5 0 0 1 1.5 1.5v11A1.5 1.5 0 0 1 14 17H6a1.5 1.5 0 0 1-1.5-1.5v-11A1.5 1.5 0 0 1 6 3z",
    "M7.5 7.5h5M7.5 10.5h5M7.5 13.5h3",
  ],
  // Sparkles: the classifier.
  ai: [
    "M8 2.5l1.3 4.2L13.5 8l-4.2 1.3L8 13.5 6.7 9.3 2.5 8l4.2-1.3z",
    "M15 12l.6 1.9 1.9.6-1.9.6L15 17l-.6-1.9-1.9-.6 1.9-.6z",
  ],
  // A picture: a slide.
  slide: [
    "M4.5 3.5h11a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1z",
    "M7.5 8.75a1.25 1.25 0 1 0 0-2.5 1.25 1.25 0 0 0 0 2.5z",
    "M3.5 14l4-4 3 3 2-2 4 4",
  ],
  // A grid of cards: the Gallery.
  gallery: ["M4 4h5v5H4zM11 4h5v5h-5zM4 11h5v5H4zM11 11h5v5h-5z"],
  mail: [
    "M4 5h12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z",
    "M3.5 6l6.5 5 6.5-5",
  ],
  lock: [
    "M6 9h8a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1z",
    "M7 9V6.5a3 3 0 0 1 6 0V9",
    "M10 12.25v1.5",
  ],
  eye: [
    "M2 10s3-5.5 8-5.5 8 5.5 8 5.5-3 5.5-8 5.5S2 10 2 10z",
    "M10 12.25a2.25 2.25 0 1 0 0-4.5 2.25 2.25 0 0 0 0 4.5z",
  ],
  eyeOff: [
    "M2 10s3-5.5 8-5.5 8 5.5 8 5.5-3 5.5-8 5.5S2 10 2 10z",
    "M10 12.25a2.25 2.25 0 1 0 0-4.5 2.25 2.25 0 0 0 0 4.5z",
    "M3.5 3.5l13 13",
  ],
};

const FEATURES = [
  ["feed", "The last 24 hours from your city's newspapers"],
  ["ai", "AI-filtered to local news, ranked by how well it will post"],
  ["slide", "Turn up to eight stories into an editable carousel"],
  ["gallery", "Every carousel saved in your Gallery"],
];

function hero() {
  return el("section", { class: "auth__hero" }, [
    el("div", { class: "auth__brand" }, [
      el("span", { class: "wordmark auth__wordmark", text: "Feedcast" }),
      el("span", { class: "auth__tagline", text: "Local stories. Bigger conversations." }),
    ]),
    el("div", { class: "auth__pitch" }, [
      el("p", { class: "auth__headline" }, [
        el("span", { text: "Discover." }),
        el("span", { text: "Create." }),
        el("span", { text: "Share what matters." }),
      ]),
      el("p", {
        class: "auth__lede",
        text: "Turn your city's newspapers into Instagram carousels, ready to post.",
      }),
      el(
        "ul",
        { class: "auth__features" },
        FEATURES.map(([icon, text]) =>
          el("li", {}, [strokeIcon(ICONS[icon], 22), el("span", { text })]),
        ),
      ),
    ]),
    // Decoration only: alt="" keeps it out of the accessibility tree.
    el("img", { class: "auth__skyline", src: "./img/skyline.svg", alt: "" }),
  ]);
}

function field({ label, icon, input, after = null }) {
  return el("label", { class: "auth__field" }, [
    el("span", { class: "auth__label", text: label }),
    el("span", { class: "auth__control" }, [
      el("span", { class: "auth__icon" }, [strokeIcon(ICONS[icon], 19)]),
      input,
      after,
    ]),
  ]);
}

export function renderLogin(root, onSuccess) {
  clear(root);

  const email = el("input", {
    class: "auth__input",
    name: "email",
    type: "email",
    placeholder: "you@example.com",
    autocomplete: "username",
    required: "",
  });
  const password = el("input", {
    class: "auth__input auth__input--toggle",
    name: "password",
    type: "password",
    placeholder: "Enter your password",
    autocomplete: "current-password",
    required: "",
  });

  // Eye-off while hidden, eye while shown — the icon shows the current state,
  // as in the design; the label says what a press will do.
  const toggle = el("button", {
    type: "button",
    class: "auth__reveal",
    "aria-label": "Show password",
    "aria-pressed": "false",
  });
  const paintToggle = () => {
    const shown = password.type === "text";
    toggle.replaceChildren(strokeIcon(shown ? ICONS.eye : ICONS.eyeOff, 19));
    toggle.setAttribute("aria-label", shown ? "Hide password" : "Show password");
    toggle.setAttribute("aria-pressed", String(shown));
  };
  toggle.addEventListener("click", () => {
    password.type = password.type === "password" ? "text" : "password";
    paintToggle();
    password.focus();
  });
  paintToggle();

  const error = el("p", { class: "auth__error", role: "alert" });
  const submit = el("button", { type: "submit", class: "auth__submit", text: "Log in" });

  const form = el("form", { class: "auth__form", novalidate: "" }, [
    el("h1", { class: "auth__title", text: "Log in to Feedcast" }),
    el("p", {
      class: "auth__subtitle",
      text: "Continue to today's stories and your saved carousels.",
    }),
    field({ label: "Email", icon: "mail", input: email }),
    field({ label: "Password", icon: "lock", input: password, after: toggle }),
    error,
    submit,
  ]);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    // novalidate turns off the browser's bubbles, which would sit over the
    // design; the same two checks are made here and shown in the one slot.
    if (!email.value.trim() || !password.value) {
      error.textContent = "Enter your email and password.";
      (email.value.trim() ? password : email).focus();
      return;
    }
    submit.disabled = true;
    try {
      const user = await apiFetch("/auth/login", {
        method: "POST",
        body: { email: email.value, password: password.value },
      });
      onSuccess(user);
    } catch (err) {
      error.textContent = err.message;
      password.value = "";
      password.focus();
    } finally {
      submit.disabled = false;
    }
  });

  root.append(
    el("div", { class: "auth" }, [hero(), el("section", { class: "auth__panel" }, [form])]),
  );
  // Only where a keyboard is already present: on a phone, focusing throws the
  // on-screen keyboard over the page before anyone has read it.
  if (window.matchMedia("(pointer: fine)").matches) email.focus();
}
