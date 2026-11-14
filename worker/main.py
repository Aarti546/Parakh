import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.models import Event
from app.services import cache as config_cache
from redis.exceptions import RedisError
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

logger = logging.getLogger("parakh.worker")
logging.basicConfig(level=logging.INFO)

GROUP = "parakh-event-writers"
CONSUMER = os.getenv("WORKER_NAME", f"worker-{uuid.uuid4().hex[:8]}")
DEAD_LETTER_SUFFIX = ":dead"
MAX_BATCH = 100
MAX_ATTEMPTS = 5


def _db_values(payload: dict[str, Any]) -> dict[str, Any]:
    values = dict(payload)
    values["event_id"] = str(values["event_id"])
    timestamp = values["created_at"]
    values["created_at"] = (
        datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else timestamp
    )
    return values


def write_batch(engine: Engine, payloads: list[dict[str, Any]]) -> None:
    if not payloads:
        return
    values = [_db_values(payload) for payload in payloads]
    dialect = engine.dialect.name
    if dialect == "postgresql":
        statement = (
            pg_insert(Event).values(values).on_conflict_do_nothing(index_elements=["event_id"])
        )
    elif dialect == "sqlite":
        statement = (
            sqlite_insert(Event).values(values).on_conflict_do_nothing(index_elements=["event_id"])
        )
    else:
        raise ValueError(f"Unsupported worker database dialect: {dialect}")
    with engine.begin() as connection:
        connection.execute(statement)


def _ack_messages(stream: str, message_ids: list[str]) -> None:
    if message_ids:
        config_cache.redis_client.xack(stream, GROUP, *message_ids)


def process_once(engine: Engine, block_ms: int = 1000) -> int:
    streams = [
        stream.decode() if isinstance(stream, bytes) else stream
        for stream in config_cache.redis_client.scan_iter(match="parakh:events:*")
        if not (stream.decode() if isinstance(stream, bytes) else stream).endswith(
            DEAD_LETTER_SUFFIX
        )
    ]
    for stream in streams:
        try:
            config_cache.redis_client.xgroup_create(stream, GROUP, id="0", mkstream=True)
        except RedisError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
    if not streams:
        return 0
    entries = config_cache.redis_client.xreadgroup(
        GROUP,
        CONSUMER,
        {stream: ">" for stream in streams},
        count=MAX_BATCH,
        block=block_ms,
    )
    for stream in streams:
        # Recover abandoned pending messages after a crashed consumer.
        claimed = config_cache.redis_client.xautoclaim(
            stream,
            GROUP,
            CONSUMER,
            min_idle_time=2_000,
            start_id="0-0",
            count=MAX_BATCH,
        )
        if claimed and claimed[1]:
            entries.append((stream, claimed[1]))
    handled = 0
    for stream_bytes, messages in entries:
        stream = stream_bytes.decode() if isinstance(stream_bytes, bytes) else stream_bytes
        good: list[tuple[str, dict[str, Any]]] = []
        poison: list[str] = []
        for message_id, fields in messages:
            message_id = message_id.decode() if isinstance(message_id, bytes) else message_id
            pending = config_cache.redis_client.xpending_range(
                stream, GROUP, message_id, message_id, 1
            )
            deliveries = pending[0].get("times_delivered", 1) if pending else 1
            if deliveries >= MAX_ATTEMPTS:
                raw_payload = fields.get(b"payload", fields.get("payload", ""))
                config_cache.redis_client.xadd(
                    stream + DEAD_LETTER_SUFFIX,
                    {
                        "original_id": message_id,
                        "payload": raw_payload,
                        "reason": "max_delivery_attempts",
                    },
                )
                poison.append(message_id)
                continue
            raw = fields.get(b"payload", fields.get("payload"))
            if isinstance(raw, bytes):
                raw = raw.decode()
            try:
                payload = json.loads(raw)
                _db_values(payload)
                good.append((message_id, payload))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                logger.exception("Malformed event moved to dead-letter stream")
                config_cache.redis_client.xadd(
                    stream + DEAD_LETTER_SUFFIX,
                    {"original_id": message_id, "payload": str(raw), "reason": "invalid_payload"},
                )
                poison.append(message_id)
        try:
            write_batch(engine, [payload for _, payload in good])
        except Exception:
            logger.exception("Event batch insert failed; leaving messages pending for retry")
            time.sleep(1)
            continue
        _ack_messages(stream, [message_id for message_id, _ in good] + poison)
        handled += len(good) + len(poison)
    return handled


def run() -> None:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    retry_delay = 1
    while True:
        try:
            process_once(engine)
            retry_delay = 1
        except RedisError:
            logger.exception("Redis worker connection failed")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)


if __name__ == "__main__":
    run()
