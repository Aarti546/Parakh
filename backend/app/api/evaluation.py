import hashlib
import json
from datetime import UTC, datetime
from secrets import token_urlsafe
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_api_user
from app.models import AuditLog, Environment, EnvironmentFlag, Flag, Project, SDKKey, User
from app.services import cache as config_cache
from app.services.evaluation import evaluate_all, evaluate_flag

router = APIRouter(tags=["evaluation"])
sdk_bearer = HTTPBearer(auto_error=False)


class EvaluateRequest(BaseModel):
    flag_key: str = Field(min_length=1, max_length=120)
    user_id: str = Field(min_length=1, max_length=200)
    attributes: dict[str, Any] = Field(default_factory=dict)


class EvaluateAllRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    attributes: dict[str, Any] = Field(default_factory=dict)


class SDKKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _sdk_environment(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(sdk_bearer)],
    request: Request,
    db: Session = Depends(get_db),
) -> Environment:
    if credentials is None or not credentials.credentials.startswith("psk_"):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_sdk_key", "message": "A valid SDK bearer key is required."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    from redis.exceptions import RedisError

    try:
        minute = int(datetime.now(UTC).timestamp()) // 60
        client_ip = request.client.host if request.client else "unknown"
        ip_key = f"parakh:rate:ip:{client_ip}:{minute}"
        ip_count = config_cache.redis_client.incr(ip_key)
        if ip_count == 1:
            config_cache.redis_client.expire(ip_key, 120)
        if ip_count > 1200:
            raise HTTPException(
                status_code=429,
                detail={"code": "rate_limited", "message": "Client request limit exceeded."},
                headers={"Retry-After": "60"},
            )
    except RedisError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "rate_limit_unavailable",
                "message": "Request limiter is unavailable.",
            },
        ) from exc
    key_record = db.scalar(
        select(SDKKey).where(
            SDKKey.key_hash == _key_digest(credentials.credentials),
            SDKKey.revoked_at.is_(None),
        )
    )
    if key_record is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_sdk_key", "message": "SDK key is invalid or revoked."},
        )
    try:
        minute = int(datetime.now(UTC).timestamp()) // 60
        rate_key = f"parakh:rate:sdk:{key_record.id}:{minute}"
        request_count = config_cache.redis_client.incr(rate_key)
        if request_count == 1:
            config_cache.redis_client.expire(rate_key, 120)
        if request_count > 600:
            raise HTTPException(
                status_code=429,
                detail={"code": "rate_limited", "message": "SDK request limit exceeded."},
                headers={"Retry-After": "60"},
            )
    except RedisError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "rate_limit_unavailable",
                "message": "Request limiter is unavailable.",
            },
        ) from exc
    environment = db.get(Environment, key_record.environment_id)
    if environment is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_sdk_key", "message": "SDK key environment no longer exists."},
        )
    return environment


def _environment_config(environment: Environment, db: Session) -> dict[str, Any]:
    cached = config_cache.get_cached_config(environment.id)
    if cached:
        return cached[0]
    rows = db.execute(
        select(Flag, EnvironmentFlag)
        .join(EnvironmentFlag, EnvironmentFlag.flag_id == Flag.id)
        .where(
            EnvironmentFlag.environment_id == environment.id,
            Flag.project_id == environment.project_id,
            Flag.archived_at.is_(None),
        )
        .order_by(Flag.key)
    )
    from app.models import Experiment

    active_experiments = {
        experiment.flag_id: {
            "key": experiment.key,
            "variants": experiment.variants,
        }
        for experiment in db.scalars(
            select(Experiment).where(
                Experiment.environment_id == environment.id,
                Experiment.status == "running",
            )
        )
    }
    configs = [
        {
            "key": flag.key,
            "name": flag.name,
            "value_type": flag.value_type,
            "default_value": flag.default_value,
            "enabled": setting.enabled,
            "kill_switch": setting.kill_switch,
            "rollout_percentage": setting.rollout_percentage,
            "targeting_rules": setting.targeting_rules,
            "on_value": setting.on_value,
            "salt": flag.key,
            "experiment": active_experiments.get(flag.id),
        }
        for flag, setting in rows
    ]
    config = {
        "project_id": environment.project_id,
        "environment": environment.key,
        "flags": configs,
    }
    config_cache.cache_config(environment.id, config)
    return config


def _owned_environment(environment_id: int, user: User, db: Session) -> Environment:
    environment = db.scalar(
        select(Environment)
        .join(Project)
        .where(Environment.id == environment_id, Project.owner_user_id == user.id)
    )
    if environment is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Environment not found."}
        )
    return environment


@router.post(
    "/api/v1/environments/{environment_id}/sdk-keys",
    status_code=status.HTTP_201_CREATED,
)
def create_sdk_key(
    environment_id: int,
    data: SDKKeyCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> dict[str, Any]:
    environment = _owned_environment(environment_id, user, db)
    secret = f"psk_{token_urlsafe(32)}"
    record = SDKKey(
        environment_id=environment.id,
        name=data.name.strip(),
        key_hash=_key_digest(secret),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "id": record.id,
        "name": record.name,
        "environment_id": record.environment_id,
        "created_at": record.created_at,
        "sdk_key": secret,
    }


@router.get("/api/v1/environments/{environment_id}/sdk-keys")
def list_sdk_keys(
    environment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> list[dict[str, Any]]:
    environment = _owned_environment(environment_id, user, db)
    return [
        {
            "id": key.id,
            "name": key.name,
            "created_at": key.created_at,
            "revoked_at": key.revoked_at,
        }
        for key in db.scalars(
            select(SDKKey)
            .where(SDKKey.environment_id == environment.id)
            .order_by(SDKKey.created_at.desc())
        )
    ]


@router.delete("/api/v1/sdk-keys/{sdk_key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_sdk_key(
    sdk_key_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> None:
    record = db.scalar(
        select(SDKKey)
        .join(Environment)
        .join(Project)
        .where(SDKKey.id == sdk_key_id, Project.owner_user_id == user.id)
    )
    if record is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "SDK key not found."}
        )
    if record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        db.commit()


@router.get("/v1/config")
def get_config(
    environment: Annotated[Environment, Depends(_sdk_environment)],
    if_none_match: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> Response:
    cached = config_cache.get_cached_config(environment.id)
    if cached:
        config, etag = cached
    else:
        config = _environment_config(environment, db)
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
        etag = hashlib.sha256(encoded.encode()).hexdigest()
    if if_none_match and if_none_match.strip('"') == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": f'"{etag}"'})
    return Response(
        content=json.dumps(config, default=str),
        media_type="application/json",
        headers={"ETag": f'"{etag}"'},
    )


@router.post("/v1/evaluate")
def evaluate_one(
    data: EvaluateRequest,
    environment: Annotated[Environment, Depends(_sdk_environment)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    config = _environment_config(environment, db)
    flag = next((item for item in config["flags"] if item["key"] == data.flag_key), None)
    if flag is None:
        raise HTTPException(
            status_code=404, detail={"code": "flag_not_found", "message": "Flag not found."}
        )
    result = evaluate_flag(flag, data.user_id, data.attributes)
    return {"flag_key": data.flag_key, **result}


@router.post("/v1/evaluate/all")
def evaluate_every_flag(
    data: EvaluateAllRequest,
    environment: Annotated[Environment, Depends(_sdk_environment)],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    config = _environment_config(environment, db)
    return {"flags": evaluate_all(config["flags"], data.user_id, data.attributes)}


@router.get("/api/v1/projects/{project_id}/audit-log")
def list_audit_log(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> list[dict[str, Any]]:
    _ = db.scalar(
        select(Project.id).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if _ is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Project not found."}
        )
    return [
        {
            "id": entry.id,
            "actor_user_id": entry.actor_user_id,
            "entity_type": entry.entity_type,
            "entity_id": entry.entity_id,
            "action": entry.action,
            "changes": entry.changes,
            "created_at": entry.created_at,
        }
        for entry in db.scalars(
            select(AuditLog)
            .where(AuditLog.project_id == project_id)
            .order_by(AuditLog.created_at.desc())
            .limit(100)
        )
    ]
