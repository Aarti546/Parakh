from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_api_user
from app.models import Environment, EnvironmentFlag, Flag, Project, User
from app.schemas.projects import EnvironmentCreate, EnvironmentRead, EnvironmentUpdate

router = APIRouter(tags=["environments"])


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


@router.get("/api/v1/projects/{project_id}/environments", response_model=list[EnvironmentRead])
def list_environments(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> list[Environment]:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if project is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Project not found."}
        )
    return list(
        db.scalars(
            select(Environment).where(Environment.project_id == project_id).order_by(Environment.id)
        )
    )


@router.post(
    "/api/v1/projects/{project_id}/environments",
    response_model=EnvironmentRead,
    status_code=status.HTTP_201_CREATED,
)
def create_environment(
    project_id: int,
    data: EnvironmentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> Environment:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if project is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Project not found."}
        )
    environment = Environment(project_id=project.id, name=data.name, key=data.key)
    db.add(environment)
    try:
        db.flush()
        flags = list(db.scalars(select(Flag).where(Flag.project_id == project_id)))
        db.add_all(
            [EnvironmentFlag(environment_id=environment.id, flag_id=flag.id) for flag in flags]
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "environment_key_exists",
                "message": "This environment key already exists.",
            },
        ) from exc
    db.refresh(environment)
    return environment


@router.patch("/api/v1/environments/{environment_id}", response_model=EnvironmentRead)
def update_environment(
    environment_id: int,
    data: EnvironmentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> Environment:
    environment = _owned_environment(environment_id, user, db)
    environment.name = data.name
    db.commit()
    db.refresh(environment)
    return environment


@router.delete("/api/v1/environments/{environment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_environment(
    environment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> None:
    environment = _owned_environment(environment_id, user, db)
    db.delete(environment)
    db.commit()
