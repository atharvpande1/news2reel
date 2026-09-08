from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from app.api.deps import get_db, require_shared_secret
from app.db.models.llm import LlmCall
from app.db.models.run import Run
from app.db.models.story import Story

router = APIRouter(prefix="/runs", tags=["reports"], dependencies=[Depends(require_shared_secret)])

_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))  # autoescape is on by default


def _safe_url(url: str | None) -> str:
    """Scheme-allowlist for every URL rendered into href/src — autoescape
    does not stop `javascript:` in an attribute. See CLAUDE.md: "External
    input is hostile"."""
    if url and (url.startswith("http://") or url.startswith("https://")):
        return url
    return "#"


templates.env.filters["safe_url"] = _safe_url


@router.get("/{run_id}/report", response_class=HTMLResponse)
def get_report(request: Request, run_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")

    selected = list(
        db.scalars(
            select(Story)
            .where(Story.run_id == run_id, Story.selected.is_(True))
            .order_by(Story.order)
        )
    )
    alternates = list(
        db.scalars(select(Story).where(Story.run_id == run_id, Story.selected.is_(False)))
    )
    alternates.sort(key=lambda s: s.scores["composite"], reverse=True)

    funneled_count = db.query(LlmCall).filter_by(run_id=run_id, attempt=1).count()
    scored_count = db.query(Story).filter_by(run_id=run_id).count()

    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "run": run,
            "selected": selected,
            "alternates": alternates,
            "funneled_count": funneled_count,
            "scored_count": scored_count,
        },
    )
