from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    logical_date: date
    attempt: int
    stage: str
    stage_started_at: datetime
    error: str | None
    error_stage: str | None
    is_current: bool
    source_outcomes: dict
    started_at: datetime
    finished_at: datetime | None
