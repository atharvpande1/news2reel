from datetime import date as date_type
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_shared_secret
from app.db.models.run import DEFAULT_TENANT_ID, Run
from app.schemas.run import RunRead
from app.services.pipeline import execute_run_in_background

router = APIRouter(prefix="/runs", tags=["runs"], dependencies=[Depends(require_shared_secret)])


@router.post("", response_model=RunRead, status_code=status.HTTP_202_ACCEPTED)
def create_run(
    background_tasks: BackgroundTasks,
    logical_date: date_type | None = None,
    force: bool = False,
    db: Session = Depends(get_db),
) -> RunRead:
    target_date = logical_date or datetime.now(tz=None).date()

    if not force:
        existing = db.scalar(
            select(Run).where(
                Run.tenant_id == DEFAULT_TENANT_ID,
                Run.logical_date == target_date,
                Run.is_current.is_(True),
            )
        )
        if existing is not None:
            return existing

    current = db.scalar(
        select(Run)
        .where(Run.tenant_id == DEFAULT_TENANT_ID, Run.logical_date == target_date)
        .order_by(Run.attempt.desc())
    )
    next_attempt = (current.attempt + 1) if current else 1

    run = Run(
        tenant_id=DEFAULT_TENANT_ID, logical_date=target_date, attempt=next_attempt, is_current=True
    )
    if current is not None:
        current.is_current = False

    db.add(run)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent POSTs raced past the SELECT above — the DB
        # constraint is the real guard. Return whichever run won.
        db.rollback()
        existing = db.scalar(
            select(Run).where(
                Run.tenant_id == DEFAULT_TENANT_ID,
                Run.logical_date == target_date,
                Run.attempt == next_attempt,
            )
        )
        return existing

    db.refresh(run)
    background_tasks.add_task(execute_run_in_background, run.id)
    return run


@router.get("", response_model=list[RunRead])
def list_runs(db: Session = Depends(get_db)) -> list[RunRead]:
    return list(db.scalars(select(Run).order_by(Run.started_at.desc())))


@router.get("/{run_id}", response_model=RunRead)
def get_run(run_id: int, db: Session = Depends(get_db)) -> RunRead:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run
