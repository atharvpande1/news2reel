"""Feed text on its way out of the database.

Lives here, not in a service, because unrelated consumers need the same policy:
the story card the browser renders, the titles that enter the classifier's
prompt, and the headline that becomes a carousel slide. Titles and summaries
arrive as raw markup soup (see services/ingest.py) and are third-party text —
CLAUDE.md's "external input is hostile" applies to every one of those paths, and
a second copy is how they drift.
"""

import re
import unicodedata

_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_markup(value: str | None) -> str:
    """Markup out, the text that was inside it left behind.

    The inbound half of the policy, split out of sanitize() because ingest needs
    the extraction without the outbound delimiter substitution below — a stored
    summary should be what the description actually said, not a value already
    shaped for a prompt it may never enter.

    Returns "" when a description carries no prose at all, which is the common
    shape of a Times of India item: the whole description is an <a><img/></a>
    thumbnail. Callers turn that into NULL rather than storing a tag soup that
    every downstream reader then has to strip again — and that the classifier
    would otherwise receive in place of a summary, spending tokens on markup and
    judging the story on its headline alone without saying so.
    """
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    return _WHITESPACE_RE.sub(" ", text).strip()


def sanitize(value: str | None, max_chars: int | None = None) -> str:
    """Strip markup, flatten whitespace, drop control characters, neutralise the
    delimiter, then truncate. `max_chars=None` skips the truncation — for
    callers like clip() below that do their own.

    Order matters — truncating first would let a title end mid-escape. The
    delimiter substitution is the half services/llm.py's build_prompt is
    missing: without it a title containing a literal </story> closes its own
    block, and everything after it reads to the model as instructions rather
    than data. It is harmless on the render path, where the same substitution
    just keeps a stray angle bracket from looking like markup.
    """
    text = strip_markup(value)
    # Lookalikes, not angle brackets: RUF001 flags them as confusable, which is
    # precisely the property wanted — a headline reading "<5% turnout" stays
    # legible while being unable to close a <story> block.
    text = text.replace("<", "‹").replace(">", "›")  # noqa: RUF001
    if max_chars is not None and len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def clip(value: str | None, max_chars: int) -> tuple[str, bool]:
    """A headline on its way onto a slide, and whether it lost words getting
    there.

    Cuts on a word boundary, unlike sanitize's own truncation: an ellipsis
    mid-token is right for a 220-character summary preview on a card and wrong
    for 66px type filling a 1080px slide. The boolean is what the canvas badges
    — a headline that quietly dropped its last words is the failure worth
    naming, and the editor is the one who can fix it.
    """
    text = sanitize(value)
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:-")
    # A single word longer than the whole box has no boundary to cut on; a hard
    # cut beats returning nothing.
    return cut or text[:max_chars], True
