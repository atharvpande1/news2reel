/* Drawing a slide. The one renderer there is.
 *
 * What the editor sees in the canvas is literally what `toDataURL` exports —
 * there is no second layout in the DOM to drift from it. Laying slides out in
 * HTML for editing and redrawing them here for export would be two
 * implementations of one design, free to disagree, and the disagreement would
 * ship as an image. Same reasoning as project_story having one implementation;
 * see CLAUDE.md's invariants. */

// 4:5. Instagram's tallest feed format, so a carousel takes the most vertical
// space in a scroll — which is the whole point of the format.
export const WIDTH = 1080;
export const HEIGHT = 1350;

/** The most text a slide can hold and still lay out.
 *
 *  Lives here rather than in slides.js because it is a property of the box this
 *  module draws, not a storage preference — widen the text box and this moves
 *  with it. Must equal settings.slide_text_max_chars, which is where the server
 *  clips a headline: clipping higher there would only hand slides.js a
 *  mid-word slice at this number instead of the word boundary it found. */
export const TEXT_MAX = 120;

// The text box. Generous side margins because the headline is the only thing on
// the slide, and the bottom margin clears Instagram's own UI overlay.
const MARGIN_X = 90;
const BOTTOM = 120;

// The accent rule under the text, and the gaps that place it. Everything is
// measured upward from the bottom, so a two-line headline and a four-line one
// share the same baseline and the deck reads as a set rather than as slides
// that wander.
const RULE_W = 96;
const RULE_H = 7;
const RULE_GAP = 38;
const CREDIT_GAP = 48;

const FONTS = {
  story: { size: 66, weight: 600, line: 1.22 },
  intro: { size: 86, weight: 700, line: 1.16 },
  cta: { size: 78, weight: 700, line: 1.18 },
};

const INK = "#17181c";
const INK_SOFT = "#5b5c62";
const ACCENT = "#b21e5f";

/** Greedy wrap against measured width, honouring explicit newlines.
 *
 *  measureText rather than a character count: headlines are proportional-font
 *  where "MMM" and "iii" are nowhere near the same width, and a character
 *  estimate overflows the box on capitals. */
function wrap(ctx, text, maxWidth) {
  const lines = [];
  for (const paragraph of String(text).split("\n")) {
    const words = paragraph.split(/\s+/).filter(Boolean);
    if (words.length === 0) {
      lines.push("");
      continue;
    }
    let line = words[0];
    for (const word of words.slice(1)) {
      const candidate = `${line} ${word}`;
      if (ctx.measureText(candidate).width <= maxWidth) line = candidate;
      else {
        lines.push(line);
        line = word;
      }
    }
    lines.push(line);
  }
  return lines;
}

/** Draws one slide onto a canvas already sized to WIDTH x HEIGHT.
 *
 *  Text sits on the bottom of the slide, per the spec, and grows upward — so a
 *  long headline pushes into the empty space above rather than off the bottom
 *  edge where Instagram's chrome would cover it. */
export function drawSlide(canvas, slide) {
  const ctx = canvas.getContext("2d");
  const font = FONTS[slide.kind] ?? FONTS.story;

  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, WIDTH, HEIGHT);

  ctx.textBaseline = "alphabetic";
  ctx.font = `${font.weight} ${font.size}px "Archivo", ui-sans-serif, system-ui, sans-serif`;
  ctx.fillStyle = INK;

  const maxWidth = WIDTH - MARGIN_X * 2;
  const lines = wrap(ctx, slide.text, maxWidth);
  const lineHeight = font.size * font.line;

  // A story slide reserves the credit line's space whether or not it has an
  // outlet to name, so the rule sits at one height across the deck.
  const creditSpace = slide.kind === "story" ? CREDIT_GAP : 0;
  const ruleTop = HEIGHT - BOTTOM - creditSpace - RULE_H;
  let y = ruleTop - RULE_GAP - (lines.length - 1) * lineHeight;

  for (const line of lines) {
    ctx.fillText(line, MARGIN_X, y);
    y += lineHeight;
  }

  // The rule is on every slide, not just the brackets: it is what makes a
  // thumbnail legible as one of ours at 7rem wide, where the text itself is
  // barely more than texture.
  ctx.fillStyle = ACCENT;
  ctx.fillRect(MARGIN_X, ruleTop, RULE_W, RULE_H);

  if (slide.kind === "story" && slide.source_name) {
    ctx.font = '500 30px "Archivo", ui-sans-serif, system-ui, sans-serif';
    ctx.fillStyle = INK_SOFT;
    ctx.fillText(slide.source_name, MARGIN_X, HEIGHT - BOTTOM);
  }
}

/** A canvas at full export size, drawn and ready for toDataURL. Detached from
 *  the document — the on-screen previews are scaled down with CSS, so nothing
 *  ever renders at display size and then has to be re-rendered bigger. */
export function renderToCanvas(slide) {
  const canvas = document.createElement("canvas");
  canvas.width = WIDTH;
  canvas.height = HEIGHT;
  drawSlide(canvas, slide);
  return canvas;
}
