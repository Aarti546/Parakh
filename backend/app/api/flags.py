from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_api_user
from app.models import Environment, EnvironmentFlag, Flag, Project, User
from app.schemas.flags import (
    EnvironmentFlagRead,
    EnvironmentFlagUpdate,
    FlagCreate,
    FlagRead,
    FlagUpdate,
    value_matches_type,
)
from app.services.audit import record_change
from app.services.cache import invalidate_project_config

router = APIRouter(tags=["flags"])


def _owned_flag(flag_id: int, user: User, db: Session) -> Flag:
    flag = db.scalar(
        select(Flag).join(Project).where(Flag.id == flag_id, Project.owner_user_id == user.id)
    )
    if flag is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Flag not found."}
        )
    return flag


@router.get("/api/v1/projects/{project_id}/flags", response_model=list[FlagRead])
def list_flags(
    project_id: int,
    include_archived: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> list[Flag]:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if project is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Project not found."}
        )
    query = select(Flag).where(Flag.project_id == project_id)
    if not include_archived:
        query = query.where(Flag.archived_at.is_(None))
    return list(db.scalars(query.order_by(Flag.key)))


@router.post(
    "/api/v1/projects/{project_id}/flags",
    response_model=FlagRead,
    status_code=status.HTTP_201_CREATED,
)
def create_flag(
    project_id: int,
    data: FlagCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> Flag:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if project is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Project not found."}
        )
    flag = Flag(
        project_id=project_id,
        key=data.key,
        name=data.name,
        description=data.description,
        value_type=data.value_type,
        default_value=data.default_value,
    )
    db.add(flag)
    try:
        db.flush()
        environments = list(
            db.scalars(select(Environment).where(Environment.project_id == project_id))
        )
        db.add_all(
            [
                EnvironmentFlag(environment_id=environment.id, flag_id=flag.id)
                for environment in environments
            ]
        )
        record_change(
            db,
            project_id=project_id,
            actor_user_id=user.id,
            entity_type="flag",
            entity_id=flag.id,
            action="created",
            changes={
                "key": {"old": None, "new": flag.key},
                "name": {"old": None, "new": flag.name},
                "description": {"old": None, "new": flag.description},
                "value_type": {"old": None, "new": flag.value_type},
                "default_value": {"old": None, "new": flag.default_value},
            },
        )
        db.commit()
        invalidate_project_config(db, project_id)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "flag_key_exists",
                "message": "A flag with this key already exists in this project.",
            },
        ) from exc
    db.refresh(flag)
    return flag


@router.get("/api/v1/flags/{flag_id}", response_model=FlagRead)
def get_flag(
    flag_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> Flag:
    return _owned_flag(flag_id, user, db)


@router.patch("/api/v1/flags/{flag_id}", response_model=FlagRead)
def update_flag(
    flag_id: int,
    data: FlagUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> Flag:
    flag = _owned_flag(flag_id, user, db)
    values = data.model_dump(exclude_unset=True)
    new_type = values.get("value_type", flag.value_type)
    new_default = values.get("default_value", flag.default_value)
    if not value_matches_type(new_default, new_type):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_default_value",
                "message": f"default_value must be a {new_type}.",
            },
        )
    changes = {
        field: {"old": getattr(flag, field), "new": value}
        for field, value in values.items()
        if getattr(flag, field) != value
    }
    for field, value in values.items():
        setattr(flag, field, value)
    if changes:
        record_change(
            db,
            project_id=flag.project_id,
            actor_user_id=user.id,
            entity_type="flag",
            entity_id=flag.id,
            action="updated",
            changes=changes,
        )
    db.commit()
    invalidate_project_config(db, flag.project_id)
    db.refresh(flag)
    return flag


@router.delete("/api/v1/flags/{flag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flag(
    flag_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> None:
    flag = _owned_flag(flag_id, user, db)
    record_change(
        db,
        project_id=flag.project_id,
        actor_user_id=user.id,
        entity_type="flag",
        entity_id=flag.id,
        action="deleted",
        changes={
            "key": {"old": flag.key, "new": None},
            "name": {"old": flag.name, "new": None},
            "description": {"old": flag.description, "new": None},
            "default_value": {"old": flag.default_value, "new": None},
        },
    )
    db.delete(flag)
    db.commit()


@router.post("/api/v1/flags/{flag_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_flag(
    flag_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> None:
    flag = _owned_flag(flag_id, user, db)
    if flag.archived_at is None:
        flag.archived_at = datetime.now(UTC)
        record_change(
            db,
            project_id=flag.project_id,
            actor_user_id=user.id,
            entity_type="flag",
            entity_id=flag.id,
            action="archived",
            changes={"archived_at": {"old": None, "new": flag.archived_at.isoformat()}},
        )
        db.commit()
        invalidate_project_config(db, flag.project_id)


def _owned_environment_flag(
    environment_id: int, flag_id: int, user: User, db: Session
) -> EnvironmentFlag:
    setting = db.scalar(
        select(EnvironmentFlag)
        .join(Environment, EnvironmentFlag.environment_id == Environment.id)
        .join(Flag, EnvironmentFlag.flag_id == Flag.id)
        .join(Project, Project.id == Environment.project_id)
        .where(
            EnvironmentFlag.environment_id == environment_id,
            EnvironmentFlag.flag_id == flag_id,
            Flag.project_id == Environment.project_id,
            Project.owner_user_id == user.id,
        )
    )
    if setting is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "Flag configuration not found."},
        )
    return setting


@router.get(
    "/api/v1/environments/{environment_id}/flags/{flag_id}",
    response_model=EnvironmentFlagRead,
)
def get_environment_flag(
    environment_id: int,
    flag_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> EnvironmentFlag:
    return _owned_environment_flag(environment_id, flag_id, user, db)


@router.patch(
    "/api/v1/environments/{environment_id}/flags/{flag_id}",
    response_model=EnvironmentFlagRead,
)
def update_environment_flag(
    environment_id: int,
    flag_id: int,
    data: EnvironmentFlagUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> EnvironmentFlag:
    setting = _owned_environment_flag(environment_id, flag_id, user, db)
    updates = data.model_dump(exclude_unset=True)
    if "on_value" in updates and not value_matches_type(
        updates["on_value"], setting.flag.value_type
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_on_value",
                "message": f"on_value must be a {setting.flag.value_type}.",
            },
        )
    changes = {}
    for field, value in updates.items():
        old_value = getattr(setting, field)
        if old_value != value:
            changes[field] = {"old": old_value, "new": value}
        setattr(setting, field, value)
    if changes:
        record_change(
            db,
            project_id=setting.environment.project_id,
            actor_user_id=user.id,
            entity_type="environment_flag",
            entity_id=setting.id,
            action="updated",
            changes=changes,
        )
    db.commit()
    invalidate_project_config(db, setting.environment.project_id)
    db.refresh(setting)
    return setting
