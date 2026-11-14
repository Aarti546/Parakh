from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.evaluation import _sdk_environment
from app.core.database import get_db
from app.models import Environment
from app.services.event_publisher import RedisStreamPublisher

router = APIRouter(tags=["events"])


class EventInput(BaseModel):
    event_id: UUID
    event_name: str = Field(min_length=1, max_length=120)
    user_id: str = Field(min_length=1, max_length=200)
    value: Any = None
    flag_key: str | None = Field(default=None, max_length=120)
    variant: str | None = Field(default=None, max_length=120)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    properties: dict[str, Any] = Field(default_factory=dict)


class EventBatch(BaseModel):
    events: list[EventInput] = Field(min_length=1, max_length=500)


@router.post("/v1/events", status_code=status.HTTP_202_ACCEPTED)
def ingest_events(
    batch: EventBatch,
    environment: Annotated[Environment, Depends(_sdk_environment)],
    db: Session = Depends(get_db),
) -> dict[str, int]:
    project_id = environment.project_id
    items = [
        {
            "event_id": str(item.event_id),
            "project_id": project_id,
            "environment_id": environment.id,
            "event_name": item.event_name,
            "user_id": item.user_id,
            "value": item.value,
            "flag_key": item.flag_key,
            "variant": item.variant,
            "created_at": item.created_at.astimezone(UTC).isoformat(),
            "properties": item.properties,
        }
        for item in batch.events
    ]
    try:
        RedisStreamPublisher().publish(environment.id, items)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "event_stream_unavailable", "message": "Events could not be queued."},
            headers={"Retry-After": "2"},
        ) from exc
    return {"accepted": len(items)}
