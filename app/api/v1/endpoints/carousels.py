"""Carousels: build one, edit it, list them, download it.

Nothing here writes a hook — a slide carries its story's headline. What this
module does own is the two moments where the editor commits to a selection, and
those are the only two that may write to `selections`. The autosave deliberately
may not: editing is not picking, and an autosave that could add rows would let a
long editing session inflate the evidence the engagement score is measured
against. See CLAUDE.md.
"""

import base64
import binascii
import io
import zipfile
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import current_user, get_db
from app.core.config import Settings, get_settings
from app.db.models.article import Article
from app.db.models.carousel import Carousel
from app.db.models.selection import SCORE_VERSION, Selection
from app.schemas.carousel import (
    CarouselRequest,
    CarouselResponse,
    CarouselSummary,
    DeckWrite,
    Slide,
    SlideWrite,
    ZipRequest,
)
from app.schemas.story import StoryRead
from app.services import carousel as carousel_service
from app.services import gallery as gallery_service
from app.services.carousel import TooManyStoriesError

router = APIRouter(prefix="/carousels", tags=["carousels"], dependencies=[Depends(current_user)])


async def _get(db: AsyncSession, carousel_id: int) -> Carousel:
    """Slides eager-loaded: save_deck replaces the collection and a delete
    cascades over it, and delete-orphan needs the old members in hand."""
    carousel = await db.get(
        Carousel, carousel_id, options=[selectinload(Carousel.slides)], populate_existing=True
    )
    if carousel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such carousel")
    return carousel


def _as_write(slides: list[Slide]) -> list[SlideWrite]:
    """Slides the server just built, in the shape save_deck stores. The narrower
    type is the point: only what cannot be recovered from the article gets
    persisted, so a score can never be frozen into a row."""
    return [
        SlideWrite(kind=s.kind, story_id=s.story_id, text=s.text, clipped=s.clipped) for s in slides
    ]


async def _response(
    db: AsyncSession, carousel: Carousel, settings: Settings, missing: list[str] | None = None
) -> CarouselResponse:
    return CarouselResponse(
        id=carousel.id,
        slides=await carousel_service.load_deck(db, carousel, settings),
        missing_ids=missing or [],
        state=carousel.state,
        created_at=carousel.created_at,
        last_edited_at=carousel.last_edited_at,
        last_downloaded_at=carousel.last_downloaded_at,
    )


async def _resolve(
    db: AsyncSession, story_ids: list[str], settings: Settings
) -> tuple[list[StoryRead], list[str], object]:
    """resolve_stories plus the two HTTP failures it can produce."""
    try:
        stories, missing, city = await carousel_service.resolve_stories(db, story_ids, settings)
    except TooManyStoriesError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    if not stories:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="none of the selected stories could be found",
        ) from None
    return stories, missing, city


def _record(
    db: AsyncSession, carousel: Carousel, stories: list[StoryRead], sort: str | None
) -> None:
    """The engagement score's only evidence — see db/models/selection.py.

    Scores are copied as the editor was shown them, never re-derived later, and
    now carry the carousel they went into: a pick that ended up in a deck
    somebody downloaded is stronger evidence than one abandoned in a draft.

    `score_version` is read off the row, never hardcoded to the current
    generation: a pick of a story the classifier has not reached yet has no
    seven-dimension score at all, and stamping one would put a number in the
    evidence trail under a formula that never produced it.
    """
    for story in stories:
        rated = story.engagement is not None
        db.add(
            Selection(
                article_id=story.id,
                engagement=story.score if rated else None,
                score=story.score,
                score_version=SCORE_VERSION if rated else None,
                sort=sort,
                carousel_id=carousel.id,
            )
        )


@router.post("", response_model=CarouselResponse, status_code=status.HTTP_201_CREATED)
async def create_carousel(
    data: CarouselRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CarouselResponse:
    """A selection becomes a carousel: an intro, one slide per story in
    engagement order, and a CTA.

    Always a new row, even with a draft already open — "Next" means start
    another one, and the old draft stays in the Gallery rather than being
    silently overwritten.

    Unknown ids come back in `missing_ids` rather than failing: the selection
    lives in the browser and can name a story that has since gone, and losing
    the other seven over it would strand the editor. Every id unknown is the
    exception — an empty deck reads as success, so that one is a 404.
    """
    stories, missing, city = await _resolve(db, data.story_ids, settings)
    if city is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="a carousel belongs to one city; this selection spans several",
        ) from None

    date = carousel_service.date_label(stories, settings)
    intro, cta = carousel_service.bracket_slides(city.name, date)
    deck = [intro, *(carousel_service.story_slide(s, settings) for s in stories), cta]

    # slides=[] so the collection is loaded from birth: save_deck replaces it,
    # and replacing an unloaded collection would be implicit IO.
    carousel = Carousel(city_id=city.id, slides=[])
    db.add(carousel)
    await db.flush()
    carousel_service.save_deck(db, carousel, _as_write(deck))
    # Deliberately after every failure path above: a deck the editor never
    # received is not a selection, and counting one would poison the measurement.
    _record(db, carousel, stories, data.sort)
    await db.commit()
    await db.refresh(carousel)
    return await _response(db, carousel, settings, missing)


@router.post("/{carousel_id}/stories", response_model=CarouselResponse)
async def add_stories(
    carousel_id: int,
    data: CarouselRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CarouselResponse:
    """More stories into a deck already on the canvas.

    Inserted before the CTA so the closing slide stays closing. The cap is on
    the deck's story slides, not on this request: eight is how many a carousel
    holds, and adding four to five already there has to fail the same way
    picking nine at once does.
    """
    carousel = await _get(db, carousel_id)
    stories, missing, _ = await _resolve(db, data.story_ids, settings)

    deck = await carousel_service.load_deck(db, carousel, settings)
    already = {s.story_id for s in deck if s.story_id}
    fresh = [s for s in stories if s.id not in already]
    existing_stories = sum(1 for s in deck if s.kind == "story")
    if existing_stories + len(fresh) > settings.carousel_max_stories:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"a carousel holds at most {settings.carousel_max_stories} stories",
        ) from None

    new_slides = [carousel_service.story_slide(s, settings) for s in fresh]
    cta = next((i for i, s in enumerate(deck) if s.kind == "cta"), len(deck))
    deck[cta:cta] = new_slides

    carousel_service.save_deck(db, carousel, _as_write(deck))
    _record(db, carousel, fresh, data.sort)
    await db.commit()
    await db.refresh(carousel)
    return await _response(db, carousel, settings, missing)


@router.put("/{carousel_id}", response_model=CarouselResponse)
async def save_carousel(
    carousel_id: int,
    data: DeckWrite,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CarouselResponse:
    """The autosave. Replaces the deck and stamps `last_edited_at`.

    Writes no Selection rows. A carousel that was already downloaded stays
    downloaded — `last_downloaded_at` is untouched here, and the Gallery reads
    the two timestamps together to say "edited since download".

    Slide story ids are checked before the write: the deck comes from the
    browser, and an id with no article behind it would otherwise reach the
    foreign key at commit and come back as a 500 rather than a refusal.
    """
    carousel = await _get(db, carousel_id)
    named = {slide.story_id for slide in data.slides if slide.story_id}
    if named:
        known = set(await db.scalars(select(Article.id).where(Article.id.in_(named))))
        if unknown := sorted(named - known):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"no such story: {', '.join(unknown)}",
            )
    carousel_service.save_deck(db, carousel, data.slides)
    await db.commit()
    await db.refresh(carousel)
    return await _response(db, carousel, settings)


@router.get("", response_model=list[CarouselSummary])
async def list_carousels(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[CarouselSummary]:
    """The Gallery: every carousel, most recently worked on first.

    No parameters. Unlike Discover it spans cities — it is an archive, not a
    feed — and it is unfiltered because the browser narrows it, which is what
    makes the filter chips' counts free. See services/gallery.py.
    """
    return await gallery_service.list_carousels(db, settings)


@router.get("/{carousel_id}", response_model=CarouselResponse)
async def read_carousel(
    carousel_id: int,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CarouselResponse:
    """One deck, to reopen on the canvas."""
    return await _response(db, await _get(db, carousel_id), settings)


@router.delete("/{carousel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_carousel(carousel_id: int, db: AsyncSession = Depends(get_db)) -> None:
    """Gone, with its slides. Its `selections` rows are not: the FK is
    ON DELETE SET NULL, because tidying the Gallery must not shrink the baseline
    the engagement score is measured against, and feeds carry only a recent
    window so those picks could never be rebuilt. See CLAUDE.md.
    """
    await db.delete(await _get(db, carousel_id))
    await db.commit()


@router.post("/{carousel_id}/zip")
async def zip_slides(
    carousel_id: int,
    data: ZipRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Bundle the rendered slides into one download, and mark the carousel
    downloaded.

    The stamp goes on only once the archive is built, so the state can never
    say a file reached someone when a cap rejected it halfway. That timestamp
    *is* the state — there is no state column to also set.

    The browser owns rendering — it is the only place the canvas exists — but
    the zip container is a solved problem in the standard library, and writing
    local headers, CRC32 and a central directory by hand in JS to avoid one
    round trip would be reinventing it.

    Base64 in a JSON body rather than multipart: file uploads would pull in
    python-multipart for something `canvas.toDataURL()` and `base64` already do
    between them, and the 33% the encoding costs is a few hundred kilobytes on a
    ten-slide deck. ZIP_STORED, not deflate — JPEG is already compressed, so
    deflating it again spends CPU to save nothing.

    An upload boundary, so it is capped the way core/net.py caps fetches: count
    and encoded length are bounded by the schema before anything is decoded, and
    the decoded total is checked as it accumulates. Nothing here parses the
    image data — it is copied through, so a malformed JPEG is the editor's
    problem, not a decoder's.
    """
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for index, encoded in enumerate(data.images, start=1):
            payload = _decode_jpeg(encoded)
            total += len(payload)
            if total > settings.zip_max_total_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="the carousel is larger than the total cap",
                )
            # Names are ours, never the client's: the numbering is what keeps
            # the slides in order once they are loose in a folder, and a name
            # from the browser could carry path separators.
            archive.writestr(f"slide-{index:02d}.jpg", payload)

    # Built, so it is going out. Only now is this carousel downloaded.
    carousel = await _get(db, carousel_id)
    carousel.last_downloaded_at = datetime.now(UTC)
    await db.commit()

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"content-disposition": 'attachment; filename="carousel.zip"'},
    )


_JPEG_PREFIX = "data:image/jpeg;base64,"


def _decode_jpeg(encoded: str) -> bytes:
    """A slide off `canvas.toDataURL("image/jpeg")`.

    The prefix is required rather than tolerated: it is the only declaration of
    type we get, and accepting a bare payload would mean zipping whatever was
    sent under a .jpg name.
    """
    if not encoded.startswith(_JPEG_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="slides must be image/jpeg data URLs",
        )
    try:
        return base64.b64decode(encoded[len(_JPEG_PREFIX) :], validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="a slide was not valid base64",
        ) from None
