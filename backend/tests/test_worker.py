import uuid
from datetime import UTC, datetime

from app.models import Base, Event
from app.services import cache
from sqlalchemy import create_engine, select

from worker.main import GROUP, process_once, write_batch


def test_worker_inserts_events_idempotently() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    event = {
        "event_id": str(uuid.uuid4()),
        "project_id": 1,
        "environment_id": 1,
        "event_name": "order_clicked",
        "user_id": "user-1",
        "value": 1,
        "flag_key": None,
        "variant": None,
        "created_at": datetime.now(UTC).isoformat(),
        "properties": {},
    }
    # Test schema needs project and environment foreign key rows before event insert.
    from app.models import Environment, Project, User

    with engine.begin() as connection:
        connection.execute(
            User.__table__.insert().values(id=1, email="a@example.com", password_hash="hash")
        )
        connection.execute(
            Project.__table__.insert().values(id=1, owner_user_id=1, name="Test", slug="test")
        )
        connection.execute(
            Environment.__table__.insert().values(id=1, project_id=1, name="Development", key="dev")
        )
    write_batch(engine, [event, event])
    with engine.connect() as connection:
        stored = connection.execute(select(Event)).all()
    assert len(stored) == 1
    engine.dispose()


def test_stream_consumer_commits_then_acknowledges(monkeypatch) -> None:
    from app.models import Environment, Project, User
    from fakeredis import FakeRedis

    fake_redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr(cache, "redis_client", fake_redis)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            User.__table__.insert().values(id=1, email="a@example.com", password_hash="hash")
        )
        connection.execute(
            Project.__table__.insert().values(id=1, owner_user_id=1, name="Test", slug="test")
        )
        connection.execute(
            Environment.__table__.insert().values(id=1, project_id=1, name="Development", key="dev")
        )
    stream = "parakh:events:1"
    payload = {
        "event_id": str(uuid.uuid4()),
        "project_id": 1,
        "environment_id": 1,
        "event_name": "order_clicked",
        "user_id": "user-1",
        "value": 1,
        "flag_key": None,
        "variant": None,
        "created_at": datetime.now(UTC).isoformat(),
        "properties": {},
    }
    fake_redis.xadd(stream, {"payload": __import__("json").dumps(payload)})
    fake_redis.xgroup_create(stream, GROUP, id="0")

    assert process_once(engine, block_ms=1) == 1
    assert fake_redis.xpending(stream, GROUP)["pending"] == 0
    with engine.connect() as connection:
        assert len(connection.execute(select(Event)).all()) == 1
    engine.dispose()
