import hashlib
import json
import logging
from json import JSONDecodeError
from typing import Any

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Environment

logger = logging.getLogger(__name__)
redis_client: Redis = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=0.25)


def cache_key(environment_id: int) -> str:
    return f"parakh:config:environment:{environment_id}"


def get_cached_config(environment_id: int) -> tuple[dict[str, Any], str] | None:
    try:
        data = redis_client.get(cache_key(environment_id))
    except RedisError:
        logger.warning("Redis config cache unavailable; reading current database config")
        return None
    if not data:
        return None
    try:
        decoded = json.loads(data)
        if not isinstance(decoded, dict):
            raise ValueError("cached config must be an object")
        config, etag = decoded.get("config"), decoded.get("etag")
        if not isinstance(config, dict) or not isinstance(etag, str):
            raise ValueError("cached config is missing config or etag")
        return config, etag
    except (JSONDecodeError, ValueError):
        logger.exception("Discarding malformed cached config for environment %s", environment_id)
        try:
            redis_client.delete(cache_key(environment_id))
        except RedisError:
            logger.warning("Could not delete malformed config cache entry")
        return None


def cache_config(environment_id: int, config: dict[str, Any]) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    etag = hashlib.sha256(encoded.encode()).hexdigest()
    try:
        redis_client.set(
            cache_key(environment_id), json.dumps({"config": config, "etag": etag}), ex=60
        )
    except RedisError:
        logger.warning("Could not write config to Redis; serving from database")
    return etag


def invalidate_environment_config(environment_id: int) -> None:
    try:
        redis_client.delete(cache_key(environment_id))
    except RedisError:
        logger.warning("Could not invalidate Redis config; short cache TTL bounds stale config")


def invalidate_project_config(db: Session, project_id: int) -> None:
    environment_ids = db.scalars(select(Environment.id).where(Environment.project_id == project_id))
    for environment_id in environment_ids:
        invalidate_environment_config(environment_id)
