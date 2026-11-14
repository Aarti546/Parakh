from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_api_user
from app.models import Environment, Project, User
from app.schemas.projects import ProjectCreate, ProjectRead, ProjectUpdate

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _owned_project(project_id: int, user: User, db: Session) -> Project:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if project is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Project not found."}
        )
    return project


@router.get("", response_model=list[ProjectRead])
def list_projects(
    db: Session = Depends(get_db), user: User = Depends(get_api_user)
) -> list[Project]:
    return list(
        db.scalars(
            select(Project)
            .where(Project.owner_user_id == user.id)
            .order_by(Project.created_at.desc())
        )
    )


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(
    data: ProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> Project:
    project = Project(owner_user_id=user.id, name=data.name, slug=data.slug)
    db.add(project)
    try:
        db.flush()
        # New projects start with useful isolated environments so flags can be managed immediately.
        db.add_all(
            [
                Environment(project_id=project.id, name="Development", key="dev"),
                Environment(project_id=project.id, name="Staging", key="staging"),
                Environment(project_id=project.id, name="Production", key="prod"),
            ]
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "slug_exists", "message": "A project with this slug already exists."},
        ) from exc
    db.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> Project:
    return _owned_project(project_id, user, db)


@router.patch("/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: int,
    data: ProjectUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> Project:
    project = _owned_project(project_id, user, db)
    project.name = data.name
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> None:
    project = _owned_project(project_id, user, db)
    db.delete(project)
    db.commit()
