from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_api_user
from app.models import Environment, EnvironmentFlag, Event, Experiment, Flag, Project, User
from app.schemas.flags import value_matches_type
from app.services.audit import record_change
from app.services.cache import invalidate_environment_config
from app.services.statistics import (
    sample_ratio_mismatch,
    sample_size_per_variant,
    two_proportion_z_test,
)

router = APIRouter(prefix="/api/v1", tags=["experiments"])


class VariantInput(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    value: Any
    traffic_percentage: float = Field(gt=0, le=100)


class ExperimentCreate(BaseModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", max_length=120)
    flag_id: int
    environment_id: int
    primary_metric: str = Field(min_length=1, max_length=120)
    variants: list[VariantInput] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def valid_traffic_split(self) -> "ExperimentCreate":
        if abs(sum(variant.traffic_percentage for variant in self.variants) - 100) > 1e-6:
            raise ValueError("variant traffic percentages must sum to 100")
        if len({variant.key for variant in self.variants}) != len(self.variants):
            raise ValueError("variant keys must be unique")
        return self


class SampleSizeInput(BaseModel):
    baseline_rate: float = Field(gt=0, lt=1)
    minimum_detectable_effect: float = Field(gt=0, lt=1)
    power: float = Field(default=0.8, gt=0, lt=1)
    alpha: float = Field(default=0.05, gt=0, lt=1)


def _owned_experiment(experiment_id: int, user: User, db: Session) -> Experiment:
    experiment = db.scalar(
        select(Experiment)
        .join(Project)
        .where(Experiment.id == experiment_id, Project.owner_user_id == user.id)
    )
    if experiment is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Experiment not found."}
        )
    return experiment


@router.post(
    "/projects/{project_id}/experiments",
    status_code=status.HTTP_201_CREATED,
)
def create_experiment(
    project_id: int,
    data: ExperimentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> dict[str, Any]:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    flag = db.scalar(select(Flag).where(Flag.id == data.flag_id, Flag.project_id == project_id))
    environment = db.scalar(
        select(Environment).where(
            Environment.id == data.environment_id,
            Environment.project_id == project_id,
        )
    )
    if project is None or flag is None or environment is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "Project, flag, or environment not found."},
        )
    setting = db.scalar(
        select(EnvironmentFlag).where(
            EnvironmentFlag.environment_id == environment.id,
            EnvironmentFlag.flag_id == flag.id,
        )
    )
    if setting is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "missing_flag_configuration",
                "message": "Flag is not configured for this environment.",
            },
        )
    if any(not value_matches_type(item.value, flag.value_type) for item in data.variants):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_variant_value",
                "message": f"Each variant value must match the flag type ({flag.value_type}).",
            },
        )
    active_experiment = db.scalar(
        select(Experiment).where(
            Experiment.flag_id == flag.id,
            Experiment.environment_id == environment.id,
            Experiment.status == "running",
        )
    )
    if active_experiment is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "experiment_already_running",
                "message": "Stop the existing experiment before creating another for this flag.",
            },
        )
    experiment = Experiment(
        project_id=project_id,
        flag_id=flag.id,
        environment_id=environment.id,
        key=data.key,
        primary_metric=data.primary_metric,
        variants=[variant.model_dump() for variant in data.variants],
        status="running",
    )
    db.add(experiment)
    setting.enabled = True
    setting.rollout_percentage = 100
    try:
        db.flush()
        record_change(
            db,
            project_id=project_id,
            actor_user_id=user.id,
            entity_type="experiment",
            entity_id=experiment.id,
            action="created",
            changes={"key": {"old": None, "new": data.key}},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "experiment_key_exists",
                "message": "Experiment key already exists for this project.",
            },
        ) from exc
    db.refresh(experiment)
    invalidate_environment_config(environment.id)
    return {
        "id": experiment.id,
        "key": experiment.key,
        "status": experiment.status,
        "variants": experiment.variants,
        "primary_metric": experiment.primary_metric,
    }


@router.get("/projects/{project_id}/experiments")
def list_experiments(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> list[dict[str, Any]]:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if project is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Project not found."}
        )
    return [
        {
            "id": exp.id,
            "key": exp.key,
            "flag_id": exp.flag_id,
            "primary_metric": exp.primary_metric,
            "status": exp.status,
            "start_at": exp.start_at,
            "end_at": exp.end_at,
        }
        for exp in db.scalars(
            select(Experiment)
            .where(Experiment.project_id == project_id)
            .order_by(Experiment.start_at.desc())
        )
    ]


@router.get("/experiments/{experiment_id}/results")
def experiment_results(
    experiment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> dict[str, Any]:
    experiment = _owned_experiment(experiment_id, user, db)
    flag = db.get(Flag, experiment.flag_id)
    if flag is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "Experiment flag not found."},
        )
    event_query = select(Event).where(
        Event.environment_id == experiment.environment_id,
        Event.created_at >= experiment.start_at,
        Event.event_name.in_(["$exposure", experiment.primary_metric]),
    )
    if experiment.end_at is not None:
        event_query = event_query.where(Event.created_at <= experiment.end_at)
    events = list(db.scalars(event_query.order_by(Event.created_at)))
    by_variant: dict[str, set[str]] = {item["key"]: set() for item in experiment.variants}
    conversions: dict[str, set[str]] = {item["key"]: set() for item in experiment.variants}
    variant_for_user: dict[str, str] = {}
    for event in events:
        if event.event_name == "$exposure" and event.variant in by_variant:
            by_variant[event.variant].add(event.user_id)
            variant_for_user[event.user_id] = event.variant
    for event in events:
        if event.event_name == experiment.primary_metric:
            variant = variant_for_user.get(event.user_id)
            if variant in conversions:
                conversions[variant].add(event.user_id)
    variant_results = []
    for variant in experiment.variants:
        key = variant["key"]
        variant_results.append(
            {
                "key": key,
                "value": variant["value"],
                "traffic_percentage": variant["traffic_percentage"],
                "users": len(by_variant[key]),
                "conversions": len(conversions[key]),
                "conversion_rate": len(conversions[key]) / len(by_variant[key])
                if by_variant[key]
                else 0,
            }
        )
    comparisons = []
    control = variant_results[0]
    for treatment in variant_results[1:]:
        if control["users"] and treatment["users"]:
            comparisons.append(
                {
                    "control": control["key"],
                    "treatment": treatment["key"],
                    **two_proportion_z_test(
                        control["conversions"],
                        control["users"],
                        treatment["conversions"],
                        treatment["users"],
                    ),
                }
            )
    srm = None
    observed = [result["users"] for result in variant_results]
    if sum(observed):
        shares = [item["traffic_percentage"] / 100 for item in experiment.variants]
        srm = sample_ratio_mismatch(observed, shares)
    daily: dict[str, dict[str, dict[str, int]]] = {}
    for event in events:
        date_key = event.created_at.date().isoformat()
        if date_key not in daily:
            daily[date_key] = {
                item["key"]: {"exposures": 0, "conversions": 0} for item in experiment.variants
            }
        variant_key = (
            event.variant
            if event.event_name == "$exposure"
            else variant_for_user.get(event.user_id)
        )
        if variant_key not in daily[date_key]:
            continue
        metric = "exposures" if event.event_name == "$exposure" else "conversions"
        daily[date_key][variant_key][metric] += 1
    return {
        "experiment_id": experiment.id,
        "key": experiment.key,
        "primary_metric": experiment.primary_metric,
        "status": experiment.status,
        "variants": variant_results,
        "comparisons": comparisons,
        "comparison": comparisons[0] if comparisons else None,
        "srm": srm,
        "daily": [{"date": date, "variants": values} for date, values in sorted(daily.items())],
        "peeking_warning": (
            "Repeatedly checking significance before the planned sample size increases "
            "false-positive risk. Treat this result as exploratory until the experiment "
            "ends or reaches its precomputed sample size."
        ),
    }


@router.post("/experiments/sample-size")
def calculate_sample_size(data: SampleSizeInput) -> dict[str, int]:
    return {
        "users_per_variant": sample_size_per_variant(
            data.baseline_rate,
            data.minimum_detectable_effect,
            data.power,
            data.alpha,
        )
    }


@router.post("/experiments/{experiment_id}/stop")
def stop_experiment(
    experiment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_api_user),
) -> dict[str, Any]:
    experiment = _owned_experiment(experiment_id, user, db)
    experiment.status = "completed"
    experiment.end_at = datetime.now(UTC)
    db.commit()
    invalidate_environment_config(experiment.environment_id)
    return {"id": experiment.id, "status": experiment.status, "end_at": experiment.end_at}
