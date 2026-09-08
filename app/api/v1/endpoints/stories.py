from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_shared_secret
from app.db.models.run import Run
from app.db.models.story import Story
from app.schemas.story import StoryRead

router = APIRouter(prefix="/runs", tags=["stories"], dependencies=[Depends(require_shared_secret)])


@router.get("/{run_id}/stories", response_model=list[StoryRead])
def get_stories(
    run_id: int,
    include: str | None = None,
    selected_only: bool = False,
    db: Session = Depends(get_db),
) -> list[StoryRead]:
    """The contract phase 2's renderer and phase 3's editor UI both consume —
    see CLAUDE.md. Default: the proposed lineup only (selected=True, in
    order). `?include=alternates` appends the alternate pool (selected=False,
    ranked by composite score). `selected_only=true` forces lineup-only even
    if `include=alternates` is also passed."""
    if db.get(Run, run_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")

    selected = list(
        db.scalars(
            select(Story)
            .where(Story.run_id == run_id, Story.selected.is_(True))
            .order_by(Story.order)
        )
    )

    want_alternates = include == "alternates" and not selected_only
    if not want_alternates:
        return selected

    # Sorted in Python, not via a JSON-path ORDER BY: SQLite's JSON query
    # support is version-dependent, and the alternate pool is small (~10).
    alternates = list(
        db.scalars(select(Story).where(Story.run_id == run_id, Story.selected.is_(False)))
    )
    alternates.sort(key=lambda s: s.scores["composite"], reverse=True)
    return selected + alternates
