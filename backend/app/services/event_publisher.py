import json
import logging
from typing import Protocol

from redis.exceptions import RedisError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import Event
from app.services import cache as config_cache

logger = logging.getLogger(__name__)


class EventPublisher(Protocol):
    def publish(self, environment_id: int, events: list[dict[str, object]]) -> None: ...


class RedisStreamPublisher:
    def publish(self, environment_id: int, events: list[dict[str, object]]) -> None:
        stream_name = f"parakh:events:{environment_id}"
        try:
            pipe = config_cache.redis_client.pipeline(transaction=False)
            for event in events:
                pipe.xadd(stream_name, {"payload": json.dumps(event, separators=(",", ":"))})
            pipe.execute()
        except RedisError as exc:
            logger.exception("Could not publish event batch to Redis stream")
            raise RuntimeError("Event stream is unavailable.") from exc


class PostgresEventPublisher:
    """Direct-to-database implementation used to compare against queued ingestion."""

    def __init__(self, db: Session, project_id: int, environment_id: int) -> None:
        self.db = db
        self.project_id = project_id
        self.environment_id = environment_id

    def publish(self, environment_id: int, events: list[dict[str, object]]) -> None:
        rows = [
            {
                **event,
                "project_id": self.project_id,
                "environment_id": self.environment_id,
            }
            for event in events
        ]
        if rows:
            dialect = self.db.get_bind().dialect.name
            if dialect == "postgresql":
                statement = (
                    pg_insert(Event)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["event_id"])
                )
            elif dialect == "sqlite":
                statement = (
                    sqlite_insert(Event)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["event_id"])
                )
            else:
                raise ValueError(f"Unsupported event database dialect: {dialect}")
            self.db.execute(statement)
            self.db.commit()
