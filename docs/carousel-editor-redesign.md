Redesign the carousel editor UI to closely match the attached reference image.

IMPORTANT: Use the attached reference image as the visual/layout reference. Do NOT generate a new image or redesign the overall UI from scratch. Reproduce the layout and proportions shown in the reference.

Current problem:
- The editing panel is too close to the slide preview vertically and feels cramped.
- The text input is too narrow/tall.
- The previous/next arrow buttons are incorrectly positioned inside the editing panel.
- The category, geographic scope, and engagement metadata are currently missing from the editing panel.

Make the following changes:

1. MAIN EDITOR LAYOUT
- Keep the vertical slide preview as the main visual focus on the left.
- Keep the editing panel on the right.
- Position the editing panel horizontally closer to the slide preview, as shown in the reference.
- The preview and editing panel should form a balanced two-column composition rather than being pushed far apart.
- Keep the large amount of whitespace around the preview, but reduce unnecessary horizontal gap between the preview and editor.

2. SLIDE NAVIGATION ARROWS
- REMOVE the previous/next arrow buttons from the editing panel header.
- Place one circular previous arrow button immediately to the LEFT of the slide preview.
- Place one circular next arrow button immediately to the RIGHT of the slide preview.
- Vertically center both buttons relative to the slide preview.
- They should visually float beside the preview, not be attached to either the preview card or the editing panel.
- Keep them circular, subtle, and consistent with the reference image.
- The arrows should navigate between slides exactly as they currently do.
- Do not duplicate the navigation controls in the editing panel.

3. EDITING PANEL
- Keep the editor panel to the RIGHT of the preview.
- Make the panel wider than the current implementation.
- Make the overall panel relatively shorter/less vertically stretched.
- The panel should feel like a compact information/editing card rather than a tall sidebar.
- Maintain the existing white card, border, radius, and subtle shadow style.

4. SLIDE TEXT INPUT
- Keep the "SLIDE TEXT" label and character counter.
- Make the textarea:
  - WIDER than the slide preview.
  - SHORTER in height than the current textarea.
  - Spacious enough to comfortably display the typical 1-3 lines of headline text.
  - Do not make it a narrow, tall textarea.
- The textarea should occupy most of the panel width with comfortable horizontal padding.
- Preserve multiline editing.
- Preserve the existing 120-character limit and character counter.
- Do NOT add "Rewrite with AI".
- Do NOT add a "Clear" or "Reset" button.
- There should be no extra editing actions below the textarea.

5. SOURCE SECTION
- Keep the SOURCE section below the text editor.
- Show the source logo/icon and source name on the left.
- Keep the "Open original article" button on the right.
- The source row should use the available horizontal space efficiently and remain compact.
- Preserve the existing functionality of opening the original article.

6. METADATA SECTION
Add the following metadata at the bottom of the editing panel:

CATEGORY
- Show the article category as a compact pill.
- Example: `crime`

ENGAGEMENT
- Show the engagement score with its existing icon.
- Example: `🚀 10.0`
- Keep the information/help icon next to the score if it already exists.

Arrange these three metadata items horizontally in three columns, separated by subtle vertical dividers, matching the reference design.

The hierarchy should be:
CATEGORY | SCOPE | ENGAGEMENT

7. PANEL HEADER
Keep:
- "Slide 2 of 6" / dynamic slide position
- "Article slide" pill

Remove:
- Previous arrow button
- Next arrow button

The header should remain compact.

8. PREVIEW
- Keep the existing slide visual design unchanged.
- The headline remains anchored toward the bottom-left of the slide.
- Keep the thin magenta accent line below the headline.
- Keep the source name below the accent line.
- Do not alter the actual slide content/layout unless necessary for positioning.

9. BOTTOM THUMBNAILS
- Keep the existing horizontal thumbnail strip.
- Keep the selected slide highlighted with the magenta border.
- Keep the Add slide card.
- Do not change the thumbnail behavior.
- The thumbnails should remain below the main editor area as in the current design.

10. OVERALL SPACING / PROPORTIONS
The desired visual relationship is:

       ← arrow     [ VERTICAL SLIDE PREVIEW ]     → arrow      [ WIDE EDITOR PANEL ]
                                                                  [ compact header    ]
                                                                  [ slide text         ]
                                                                  [ source             ]
                                                                  [ category | scope | engagement ]

The arrows belong to the preview/navigation area, NOT the editor panel.

The editor panel should be:
- wider
- shorter
- closer to the preview
- less vertically cramped
- horizontally aligned with the main preview

11. RESPONSIVENESS
- Preserve responsive behavior.
- On desktop, use the two-column layout described above.
- On smaller screens, stack/reflow naturally rather than allowing the panel to become unusably narrow.
- Do not break the existing editor functionality.

12. DO NOT CHANGE
Do not change:
- slide data/model
- slide navigation logic
- text editing behavior
- autosave behavior
- source article functionality
- thumbnail functionality
- download functionality
- existing color palette
- existing overall visual identity

This is primarily a layout and component-positioning change.

Use the attached reference image as the source of truth for the visual proportions, spacing, arrow placement, panel width, and overall composition.