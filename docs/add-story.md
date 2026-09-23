Redesign the "Add Story / Pick from news feed" workflow in the carousel/reel editor.

IMPORTANT: Do not navigate the user back to the Discover page when they click "Add stories". The editor must remain the user's primary context.

The current application has:
- A Discover page with a news feed, category filters, geographic-scope filters, and sort controls.
- A carousel/reel editor with a slide preview, editable slide text, source information, and a bottom thumbnail strip.
- An "+ Add slide" / "Add stories" action in the editor.
- News articles have category, geographic scope, engagement score, source, timestamp, headline, excerpt, and URL.
- The existing "Pick from the news feed" UI is a small modal containing a plain checkbox list.
- The existing Discover feed uses larger story cards.

Redesign the story-selection workflow using these UX principles:

1. KEEP THE USER INSIDE THE EDITOR
When the user clicks "+ Add stories", open a large modal or sheet over the editor (if the user picks "pick from the news feed" option). Do NOT route to /discover and do NOT replace the editor page.

The editor should remain mounted underneath the modal so that:
- current slide remains selected
- current editor state is preserved
- unsaved text changes are preserved
- closing/cancelling the picker immediately returns the user to exactly where they were

2. USE A LARGE STORY PICKER, NOT THE CURRENT SMALL MODAL
The picker should occupy roughly 75-90% of the viewport width and enough vertical space to display a useful number of stories.

Use the existing minimal visual language:
- light gray/beige page background
- white surfaces
- thin subtle borders
- restrained shadows
- existing magenta accent
- existing typography
- no unnecessary decorative elements

Do not redesign the overall application visual identity.

3. HEADER
At the top of the modal:

"Add stories"

Under it:
"Select up to 4 stories to add to this carousel"

On the right:
close button

Also show the current selection count prominently, e.g.:

"2 / 4 selected"

4. FILTERS
Reuse the existing Discover filtering concepts, but make them compact.

Include:
- Search stories
- Category
- Geographic scope
- Sort by

Do not reproduce the entire Discover toolbar verbatim.

Use compact controls such as dropdowns/chips where appropriate.

The default sorting should remain the same as the current Discover context unless there is already an established editor-specific default.

5. STORY RESULTS SHOULD USE A COMPACT LIST, NOT LARGE DISCOVER CARDS

This is important.

The purpose here is fast selection, not browsing.

Each story row should show:

- selection checkbox/check indicator
- geographic scope badge
- category badge
- headline
- source
- relative timestamp
- compact engagement score

Example:

[✓]  crime                                     10.0
     Drunk ex-partner tries to barge into
     woman's house in Nagpur

     Times of India · 15h ago

Do not show large excerpts unless there is enough space and they materially help selection.

The headline should be the visual priority.

5. SELECTION BEHAVIOR

Make the entire story row clickable.

When selected:
- show a clear magenta border/accent
- show a checkmark
- update the selection counter

Do not rely solely on the checkbox to communicate selection.

Respect the existing maximum number of selectable stories.

Disable further selection when the maximum is reached, while still allowing already-selected stories to be deselected.

6. STICKY FOOTER

The bottom of the modal should have a sticky footer.

Left:
"2 / 4 selected"

Right:
"Cancel"
"Add stories"

The primary Add button should be disabled when zero stories are selected.

When clicked:
- add the selected stories as article slides to the current carousel
- close the picker
- return to the editor
- preserve the current editor state
- make the newly added slides available in the thumbnail strip

7. KEEP DISCOVER AS A SEPARATE EXPERIENCE

Do NOT make the picker a duplicate of the full Discover page.

Add a subtle secondary link/button somewhere in the picker:

"Browse full Discover feed →"

This should be the only path that takes the user to the Discover page.

The distinction should be:

Discover = explore and discover stories

Add stories = quickly select stories for the current carousel

8. REUSE EXISTING DATA AND FILTERING LOGIC

Do not create a second independent implementation of:
- story fetching
- category filtering
- geographic filtering
- sorting
- engagement scoring
- source metadata

Reuse the existing Discover/feed APIs, hooks, types, and data structures where possible.

Only create a different presentation/interaction layer for the story-picker.

9. RESPONSIVE BEHAVIOR

Desktop is the primary target.

On desktop:
- large centered modal/sheet
- filters near the top
- scrollable story list
- sticky footer

On narrower screens:
- modal can become nearly full-screen
- filters can wrap or collapse
- story rows should remain easy to scan

10. DO NOT CHANGE THE CAROUSEL EDITOR ITSELF

This task is specifically about the Add Stories workflow.

Do not redesign:
- the slide preview
- slide text editor
- bottom thumbnail strip
- source panel
- download controls
- existing editor layout

Only make the minimum integration changes needed to open/close the story picker and add selected stories.

11. IMPORTANT UX DETAIL

If the user opens the picker while editing slide 3, then cancels it, they must return to slide 3 exactly as they left it.

If they add stories, return to the editor with the newly added article slides visible in the thumbnail strip.

Do not reload or navigate the entire editor page.

12. VISUAL HIERARCHY

Prioritize:

1. Headline
2. Selection state
3. Source/time
4. Category/scope
5. Engagement score

Do not make engagement score visually dominant in the picker.

The Discover feed can use the larger engagement-score treatment, but the story picker should treat engagement as secondary metadata.

Implement this using the existing component architecture and styling system. Before creating new components, inspect and reuse existing StoryCard, filters, story-list, modal/dialog, badge, and button components where appropriate.