"""The /ui mount. Only the wiring is testable here — there is no JS runner in
this project — but the mount is exactly the part that breaks silently: a
misplaced mount shadows /api/v1 and every API call starts returning HTML."""

import re

import pytest
from httpx import AsyncClient

from app.core.config import Settings
from app.core.enums import ENGAGEMENT_DIMENSIONS, Category, EngagementLevel
from app.services.rank import engagement_weights

MODULES = [
    "main.js",
    "api.js",
    "selection.js",
    "city.js",
    "picker.js",
    "feed.js",
    "story-picker.js",
    "gallery.js",
    "slides.js",
    "slide-canvas.js",
    "studio.js",
    "cities.js",
    "sources.js",
    "login.js",
]


async def test_ui_index_is_served(client: AsyncClient) -> None:
    response = await client.get("/ui/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Feedcast" in response.text


@pytest.mark.parametrize("module", MODULES)
async def test_ui_serves_every_module(client: AsyncClient, module: str) -> None:
    """A renamed or missing module leaves a dead `import` that fails only in the
    browser — nothing server-side errors."""
    response = await client.get(f"/ui/js/{module}")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


async def test_the_masthead_carries_the_city_picker_markup(client: AsyncClient) -> None:
    """picker.js resolves #city-picker and #city-name at module scope and calls
    addEventListener on the result. Delete either from the markup and mounting
    throws a TypeError at boot, which blanks the whole app while the server goes
    on reporting that everything is fine."""
    body = (await client.get("/ui/")).text
    assert 'id="city-picker"' in body
    assert 'id="city-name"' in body


async def test_ui_serves_the_stylesheet(client: AsyncClient) -> None:
    response = await client.get("/ui/css/app.css")
    assert response.status_code == 200
    assert "css" in response.headers["content-type"]


@pytest.mark.parametrize("path", ["/ui/", "/ui/css/app.css", "/ui/js/feed.js"])
async def test_static_assets_must_be_revalidated(client: AsyncClient, path: str) -> None:
    """Without Cache-Control, StaticFiles' ETag alone lets a browser cache
    heuristically — and it does, hardest of all for ES modules. The failure is
    vicious to diagnose: fresh HTML and CSS driven by a previous version of the
    JS, which renders as "the CSS is broken and nothing is clickable"."""
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


async def test_revalidation_is_cheap(client: AsyncClient) -> None:
    """no-cache means revalidate, not re-download: an unchanged file is a 304
    with no body, so the header costs a round trip and not the bytes."""
    first = await client.get("/ui/js/feed.js")
    again = await client.get("/ui/js/feed.js", headers={"if-none-match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.content == b""


async def test_root_redirects_to_the_ui(client: AsyncClient) -> None:
    response = await client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/ui/"


async def test_mount_does_not_shadow_the_api(client: AsyncClient) -> None:
    """The reason the mount is at /ui and registered last."""
    assert (await client.get("/api/v1/stories")).status_code == 200
    assert (await client.get("/api/v1/sources")).status_code == 200
    assert (await client.get("/api/v1/cities")).status_code == 200
    assert (await client.get("/health")).status_code == 200

    # Asserting the content type, not the status: an all-unknown selection is a
    # legitimate 404, and the content type is what tells "the route answered"
    # apart from "the static mount served index.html".
    response = await client.post("/api/v1/carousel", json={"story_ids": ["nope"]})
    assert "application/json" in response.headers["content-type"]
    assert response.status_code == 404


async def test_the_category_list_matches_the_enum(client: AsyncClient) -> None:
    """The category values are hand-copied into feed.js, and nothing else checks
    that the two agree. A value in the enum but not the array is a filter chip
    that never appears; a value in the array but not the enum is a chip whose
    request comes back 422. Both are browser-only, and neither raises anywhere.

    Order too, not just membership: the chip row reads as a list and the enum is
    the one place that decides what order that is.
    """
    served = (await client.get("/ui/js/feed.js")).text
    block = re.search(r"export const CATEGORIES = \[(.*?)\];", served, re.S)
    assert block is not None, "CATEGORIES array not found — did it get renamed?"

    assert re.findall(r'"([a-z_]+)"', block.group(1)) == [c.value for c in Category]


async def test_every_category_colour_names_a_real_category(client: AsyncClient) -> None:
    """A subset, deliberately, not equality: only the values that carry volume
    get a token and the rest fall through to the neutral base .tag, as `other`
    always has. What must not happen is a token for a value that no longer
    exists — dead CSS that looks like live styling."""
    served = (await client.get("/ui/css/app.css")).text
    tokens = set(re.findall(r"^\.tag--([a-z_]+)", served, re.M))
    assert tokens <= {c.value for c in Category}


async def test_the_more_panel_holds_exactly_the_uncommon_categories(client: AsyncClient) -> None:
    """Sixteen chips do not fit a row, so the uncommon ones live behind a
    <details>. The split is by name, so a category added to the enum and to
    CATEGORIES but forgotten here would silently vanish from the filter — the
    chips are the only way to reach it."""
    served = (await client.get("/ui/js/feed.js")).text
    block = re.search(r"const PRIMARY_CATEGORIES = new Set\(\[(.*?)\]\)", served, re.S)
    assert block is not None, "PRIMARY_CATEGORIES not found — did it get renamed?"

    primary = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    every = {c.value for c in Category}
    # Every primary names a real category, and the two sets together are the
    # whole enum — nothing can fall between the row and the panel.
    assert primary <= every
    assert primary | (every - primary) == every
    # "other" is a fallback, never a chip an editor reaches for first.
    assert "other" not in primary


async def test_the_engagement_dimensions_match_the_enum(client: AsyncClient) -> None:
    """The seven dimension names are hand-copied into feed.js to build the
    score hover, and nothing else checks the two agree. The map this replaced
    was a three-tier impact list that fell back to printing the raw slug, so
    when the enum changed the hover quietly started reading "tier_2" mid-
    sentence. Browser-only, and it never raised.

    Order too: the array is in weight order and the hover ranks off it.
    """
    served = (await client.get("/ui/js/feed.js")).text
    block = re.search(r"export const ENGAGEMENT_DIMENSIONS = \[(.*?)\];", served, re.S)
    assert block is not None, "ENGAGEMENT_DIMENSIONS not found — did it get renamed?"

    names = re.findall(r'\["([a-z_]+)",', block.group(1))
    assert names == list(ENGAGEMENT_DIMENSIONS)


async def test_the_engagement_weights_match_config(client: AsyncClient) -> None:
    """The weights are duplicated into feed.js to order the hover's
    contributors. A stale copy misranks the explanation of a score rather than
    the score itself — still wrong, and still invisible from Python."""
    served = (await client.get("/ui/js/feed.js")).text
    block = re.search(r"export const ENGAGEMENT_DIMENSIONS = \[(.*?)\];", served, re.S)
    served_weights = {
        name: float(weight)
        for name, weight in re.findall(r'\["([a-z_]+)", ([\d.]+),', block.group(1))
    }
    assert served_weights == engagement_weights(Settings())


async def test_the_engagement_levels_match_the_enum(client: AsyncClient) -> None:
    """feed.js maps each level to a word for the hover and to a number for
    ordering. A level in the enum but missing from either renders "undefined"
    in a tooltip, or silently contributes zero to the ranking."""
    served = (await client.get("/ui/js/feed.js")).text
    for name in ("LEVEL_WORD", "LEVEL_VALUES"):
        block = re.search(rf"const {name} = \{{(.*?)\}};", served, re.S)
        assert block is not None, f"{name} not found — did it get renamed?"
        keys = set(re.findall(r"(\w+):", block.group(1)))
        assert keys == {level.value for level in EngagementLevel}, name


async def test_ui_serves_the_login_skyline(client: AsyncClient) -> None:
    """login.js points an <img> at it; a missing file is a broken-image icon
    at the foot of the one page every editor sees first."""
    response = await client.get("/ui/img/skyline.svg")
    assert response.status_code == 200
    assert "svg" in response.headers["content-type"]
