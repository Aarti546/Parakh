import json
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.evaluation import _key_digest
from app.api.experiments import SampleSizeInput, experiment_results
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_dashboard_user
from app.core.security import (
    create_access_token,
    hash_password,
    new_csrf_token,
    set_auth_cookies,
    verify_csrf_token,
    verify_password,
)
from app.models import (
    AuditLog,
    Environment,
    EnvironmentFlag,
    Experiment,
    Flag,
    Project,
    SDKKey,
    User,
)
from app.schemas.flags import EnvironmentFlagUpdate, value_matches_type
from app.services.audit import record_change
from app.services.cache import invalidate_project_config
from app.services.statistics import sample_size_per_variant

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


def _csrf_token(request: Request) -> str:
    return request.cookies.get("parakh_csrf_token") or new_csrf_token()


def _set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        "parakh_csrf_token",
        token,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.access_token_minutes * 60,
        path="/",
    )


def _verify_csrf(request: Request, submitted: str) -> None:
    cookie_token = request.cookies.get("parakh_csrf_token", "")
    if (
        not cookie_token
        or not submitted
        or not compare_digest(cookie_token, submitted)
        or not verify_csrf_token(submitted)
    ):
        raise HTTPException(status_code=403, detail="CSRF token validation failed.")


def _html_error(
    request: Request,
    template: str,
    message: str,
    status_code: int = 400,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name=template,
        context={"csrf_token": _csrf_token(request), "error": message},
        status_code=status_code,
    )
    _set_csrf_cookie(response, _csrf_token(request))
    return response


@router.get("/", include_in_schema=False)
def home(request: Request) -> RedirectResponse:
    destination = "/projects" if request.cookies.get("parakh_access_token") else "/login"
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request) -> HTMLResponse:
    token = _csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": token, "error": None},
    )
    _set_csrf_cookie(response, token)
    return response


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
def login_form(
    request: Request,
    response: Response,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    db: Session = Depends(get_db),
) -> Response:
    _verify_csrf(request, csrf_token)
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not verify_password(password, user.password_hash):
        return _html_error(request, "login.html", "Email or password is incorrect.", 401)
    redirect = RedirectResponse("/projects", status_code=status.HTTP_303_SEE_OTHER)
    set_auth_cookies(redirect, create_access_token(user.id))
    return redirect


@router.get("/register", response_class=HTMLResponse, include_in_schema=False)
def register_page(request: Request) -> HTMLResponse:
    token = _csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"csrf_token": token, "error": None},
    )
    _set_csrf_cookie(response, token)
    return response


@router.post("/register", response_class=HTMLResponse, include_in_schema=False)
def register_form(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    db: Session = Depends(get_db),
) -> Response:
    _verify_csrf(request, csrf_token)
    normalized_email = email.strip().lower()
    if len(password) < 8 or len(password) > 128:
        return _html_error(request, "register.html", "Password must be 8–128 characters.")
    if "@" not in normalized_email:
        return _html_error(request, "register.html", "Enter a valid email address.")
    user = User(email=normalized_email, password_hash=hash_password(password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _html_error(
            request, "register.html", "An account with this email already exists.", 409
        )
    db.refresh(user)
    redirect = RedirectResponse("/projects", status_code=status.HTTP_303_SEE_OTHER)
    set_auth_cookies(redirect, create_access_token(user.id))
    return redirect


@router.post("/logout", include_in_schema=False)
def logout_form(
    request: Request,
    csrf_token: Annotated[str, Form()],
) -> RedirectResponse:
    _verify_csrf(request, csrf_token)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("parakh_access_token", path="/", samesite="lax")
    response.delete_cookie("parakh_csrf_token", path="/", samesite="lax")
    return response


@router.get("/projects", response_class=HTMLResponse, include_in_schema=False)
def projects_page(
    request: Request,
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    projects = list(
        db.scalars(
            select(Project)
            .where(Project.owner_user_id == user.id)
            .order_by(Project.created_at.desc())
        )
    )
    token = _csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="projects.html",
        context={"user": user, "projects": projects, "csrf_token": token},
    )
    _set_csrf_cookie(response, token)
    return response


@router.post("/projects", include_in_schema=False)
def create_project_form(
    request: Request,
    name: Annotated[str, Form()],
    slug: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> Response:
    _verify_csrf(request, csrf_token)
    normalized_slug = slug.strip().lower()
    if not normalized_slug or any(
        c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in normalized_slug
    ):
        return _html_error(
            request,
            "error.html",
            "Use a project slug containing lowercase letters, numbers, and hyphens.",
        )
    project = Project(owner_user_id=user.id, name=name.strip(), slug=normalized_slug)
    db.add(project)
    try:
        db.flush()
        db.add_all(
            [
                Environment(project_id=project.id, name="Development", key="dev"),
                Environment(project_id=project.id, name="Staging", key="staging"),
                Environment(project_id=project.id, name="Production", key="prod"),
            ]
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        return _html_error(request, "error.html", "That project slug is already in use.", 409)
    return RedirectResponse(f"/projects/{project.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/projects/{project_id}", response_class=HTMLResponse, include_in_schema=False)
def project_page(
    project_id: int,
    request: Request,
    environment_id: int | None = None,
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    environments = list(
        db.scalars(
            select(Environment).where(Environment.project_id == project.id).order_by(Environment.id)
        )
    )
    selected_environment = next(
        (environment for environment in environments if environment.id == environment_id),
        environments[0] if environments else None,
    )
    flags: list[dict[str, Any]] = []
    if selected_environment:
        results = db.execute(
            select(Flag, EnvironmentFlag)
            .outerjoin(
                EnvironmentFlag,
                (EnvironmentFlag.flag_id == Flag.id)
                & (EnvironmentFlag.environment_id == selected_environment.id),
            )
            .where(Flag.project_id == project.id, Flag.archived_at.is_(None))
            .order_by(Flag.key)
        )
        flags = [{"flag": flag, "setting": setting} for flag, setting in results]
    token = _csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="project_detail.html",
        context={
            "user": user,
            "project": project,
            "environments": environments,
            "selected_environment": selected_environment,
            "flags": flags,
            "csrf_token": token,
        },
    )
    _set_csrf_cookie(response, token)
    return response


@router.post("/projects/{project_id}/flags", include_in_schema=False)
def create_flag_form(
    project_id: int,
    request: Request,
    key: Annotated[str, Form()],
    name: Annotated[str, Form()],
    value_type: Annotated[str, Form()],
    default_value: Annotated[str, Form()],
    environment_id: Annotated[int, Form()],
    csrf_token: Annotated[str, Form()],
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> Response:
    _verify_csrf(request, csrf_token)
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    environment = db.scalar(
        select(Environment).where(
            Environment.id == environment_id, Environment.project_id == project_id
        )
    )
    if project is None or environment is None:
        raise HTTPException(status_code=404, detail="Project or environment not found.")
    if value_type == "boolean":
        parsed_value: Any = default_value.lower() == "true"
        valid = default_value.lower() in {"true", "false"}
    elif value_type == "number":
        try:
            parsed_value = float(default_value)
            valid = isfinite(parsed_value)
        except ValueError:
            parsed_value = None
            valid = False
    elif value_type == "string":
        parsed_value = default_value
        valid = True
    else:
        parsed_value = None
        valid = False
    if not valid:
        return _html_error(request, "error.html", "Default value does not match the selected type.")
    normalized_key = key.strip()
    if not normalized_key or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for c in normalized_key
    ):
        return _html_error(
            request,
            "error.html",
            "Use only letters, numbers, underscores, and hyphens in the flag key.",
        )
    flag = Flag(
        project_id=project_id,
        key=normalized_key,
        name=name.strip(),
        value_type=value_type,
        default_value=parsed_value,
    )
    db.add(flag)
    try:
        db.flush()
        environments = list(
            db.scalars(select(Environment).where(Environment.project_id == project_id))
        )
        db.add_all(
            [EnvironmentFlag(environment_id=env.id, flag_id=flag.id) for env in environments]
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        return _html_error(
            request, "error.html", "That flag key already exists in this project.", 409
        )
    return RedirectResponse(
        f"/projects/{project_id}?environment_id={environment_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/projects/{project_id}/flags/{flag_id}/toggle", include_in_schema=False)
def toggle_flag_form(
    project_id: int,
    flag_id: int,
    request: Request,
    environment_id: Annotated[int, Form()],
    csrf_token: Annotated[str, Form()],
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
    hx_request: Annotated[str | None, Header(alias="HX-Request")] = None,
) -> Response:
    _verify_csrf(request, csrf_token)
    setting = db.scalar(
        select(EnvironmentFlag)
        .join(Environment, EnvironmentFlag.environment_id == Environment.id)
        .join(Flag, EnvironmentFlag.flag_id == Flag.id)
        .join(Project, Project.id == Environment.project_id)
        .where(
            EnvironmentFlag.flag_id == flag_id,
            EnvironmentFlag.environment_id == environment_id,
            Flag.project_id == project_id,
            Environment.project_id == project_id,
            Project.owner_user_id == user.id,
        )
    )
    if setting is None:
        raise HTTPException(status_code=404, detail="Flag configuration not found.")
    setting.enabled = not setting.enabled
    record_change(
        db,
        project_id=project_id,
        actor_user_id=user.id,
        entity_type="environment_flag",
        entity_id=setting.id,
        action="updated",
        changes={"enabled": {"old": not setting.enabled, "new": setting.enabled}},
    )
    db.commit()
    invalidate_project_config(db, project_id)
    if hx_request == "true":
        label = "Enabled" if setting.enabled else "Disabled"
        css_class = "enabled" if setting.enabled else "disabled"
        action = f"/projects/{project_id}/flags/{flag_id}/toggle"
        return HTMLResponse(
            f'<div id="flag-status-{flag_id}"><span class="status {css_class}">{label}</span>'
            f'<form class="inline-form" method="post" action="{action}" hx-post="{action}" '
            f'hx-target="#flag-status-{flag_id}" hx-swap="outerHTML">'
            f'<input type="hidden" name="csrf_token" value="{csrf_token}">'
            f'<input type="hidden" name="environment_id" value="{environment_id}">'
            f'<button class="button small" type="submit">'
            f"{'Disable' if setting.enabled else 'Enable'}</button></form></div>"
        )
    return RedirectResponse(
        f"/projects/{project_id}?environment_id={environment_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/projects/{project_id}/flags/{flag_id}/settings",
    include_in_schema=False,
)
def update_flag_settings_form(
    project_id: int,
    flag_id: int,
    request: Request,
    environment_id: Annotated[int, Form()],
    enabled: Annotated[bool, Form()],
    kill_switch: Annotated[bool, Form()],
    rollout_percentage: Annotated[float, Form(ge=0, le=100)],
    targeting_rules: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    on_value: Annotated[str, Form()] = "",
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
    hx_request: Annotated[str | None, Header(alias="HX-Request")] = None,
) -> Response:
    _verify_csrf(request, csrf_token)
    setting = db.scalar(
        select(EnvironmentFlag)
        .join(Environment, EnvironmentFlag.environment_id == Environment.id)
        .join(Flag, EnvironmentFlag.flag_id == Flag.id)
        .join(Project, Project.id == Environment.project_id)
        .where(
            EnvironmentFlag.flag_id == flag_id,
            EnvironmentFlag.environment_id == environment_id,
            Flag.project_id == project_id,
            Project.owner_user_id == user.id,
        )
    )
    if setting is None:
        raise HTTPException(status_code=404, detail="Flag configuration not found.")
    try:
        rules = json.loads(targeting_rules) if targeting_rules.strip() else None
        flag = db.get(Flag, flag_id)
        if flag is None:
            raise HTTPException(status_code=404, detail="Flag not found.")
        if on_value == "":
            parsed_on_value = None
        elif flag.value_type == "boolean":
            if on_value.lower() not in {"true", "false"}:
                raise ValueError
            parsed_on_value = on_value.lower() == "true"
        elif flag.value_type == "number":
            parsed_on_value = float(on_value)
            if not isfinite(parsed_on_value):
                raise ValueError
        else:
            parsed_on_value = on_value
        update = EnvironmentFlagUpdate(
            enabled=enabled,
            kill_switch=kill_switch,
            rollout_percentage=rollout_percentage,
            targeting_rules=rules,
            **({"on_value": parsed_on_value} if on_value != "" else {}),
        )
    except (json.JSONDecodeError, ValueError):
        if hx_request == "true":
            return HTMLResponse(
                '<span class="alert">Invalid targeting rule JSON.</span>', status_code=422
            )
        return _html_error(request, "error.html", "Targeting rules must be valid JSON.")
    changes = {}
    for field, value in update.model_dump(exclude_unset=True).items():
        old_value = getattr(setting, field)
        if old_value != value:
            changes[field] = {"old": old_value, "new": value}
        setattr(setting, field, value)
    if changes:
        record_change(
            db,
            project_id=project_id,
            actor_user_id=user.id,
            entity_type="environment_flag",
            entity_id=setting.id,
            action="updated",
            changes=changes,
        )
    db.commit()
    invalidate_project_config(db, project_id)
    if hx_request == "true":
        return HTMLResponse('<span class="status enabled">Saved</span>')
    return RedirectResponse(
        f"/projects/{project_id}?environment_id={environment_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/projects/{project_id}/flags/{flag_id}/archive", include_in_schema=False)
def archive_flag_form(
    project_id: int,
    flag_id: int,
    request: Request,
    csrf_token: Annotated[str, Form()],
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> Response:
    _verify_csrf(request, csrf_token)
    flag = db.scalar(
        select(Flag)
        .join(Project)
        .where(
            Flag.id == flag_id,
            Flag.project_id == project_id,
            Project.owner_user_id == user.id,
        )
    )
    if flag is None:
        raise HTTPException(status_code=404, detail="Flag not found.")
    if flag.archived_at is None:
        flag.archived_at = datetime.now(UTC)
        record_change(
            db,
            project_id=project_id,
            actor_user_id=user.id,
            entity_type="flag",
            entity_id=flag.id,
            action="archived",
            changes={"archived_at": {"old": None, "new": flag.archived_at.isoformat()}},
        )
        db.commit()
        invalidate_project_config(db, project_id)
    return RedirectResponse(
        f"/projects/{project_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    "/projects/{project_id}/flags/{flag_id}", response_class=HTMLResponse, include_in_schema=False
)
def flag_page(
    project_id: int,
    flag_id: int,
    request: Request,
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    flag = db.scalar(
        select(Flag)
        .join(Project)
        .where(
            Flag.id == flag_id,
            Flag.project_id == project_id,
            Project.owner_user_id == user.id,
        )
    )
    if flag is None:
        raise HTTPException(status_code=404, detail="Flag not found.")
    token = _csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="flag_detail.html",
        context={"user": user, "flag": flag, "csrf_token": token},
    )
    _set_csrf_cookie(response, token)
    return response


@router.get(
    "/projects/{project_id}/experiments",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def experiments_page(
    project_id: int,
    request: Request,
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    experiments = list(
        db.scalars(
            select(Experiment)
            .where(Experiment.project_id == project_id)
            .order_by(Experiment.start_at.desc())
        )
    )
    flags = list(
        db.scalars(select(Flag).where(Flag.project_id == project_id, Flag.archived_at.is_(None)))
    )
    environments = list(db.scalars(select(Environment).where(Environment.project_id == project_id)))
    token = _csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="experiments.html",
        context={
            "user": user,
            "project": project,
            "experiments": experiments,
            "flags": flags,
            "environments": environments,
            "csrf_token": token,
        },
    )
    _set_csrf_cookie(response, token)
    return response


@router.post("/projects/{project_id}/experiments", include_in_schema=False)
def create_experiment_form(
    project_id: int,
    request: Request,
    key: Annotated[str, Form()],
    flag_id: Annotated[int, Form()],
    environment_id: Annotated[int, Form()],
    primary_metric: Annotated[str, Form()],
    control_value: Annotated[str, Form()],
    treatment_value: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> Response:
    _verify_csrf(request, csrf_token)
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    flag = db.scalar(select(Flag).where(Flag.id == flag_id, Flag.project_id == project_id))
    environment = db.scalar(
        select(Environment).where(
            Environment.id == environment_id, Environment.project_id == project_id
        )
    )
    if project is None or flag is None or environment is None:
        raise HTTPException(status_code=404, detail="Project, flag, or environment not found.")
    active_experiment = db.scalar(
        select(Experiment).where(
            Experiment.flag_id == flag.id,
            Experiment.environment_id == environment.id,
            Experiment.status == "running",
        )
    )
    if active_experiment is not None:
        return _html_error(
            request,
            "error.html",
            "Stop the existing experiment for this flag before creating another.",
            409,
        )
    try:
        if flag.value_type == "boolean":
            values: list[Any] = [control_value.lower() == "true", treatment_value.lower() == "true"]
            if control_value.lower() not in {"true", "false"} or treatment_value.lower() not in {
                "true",
                "false",
            }:
                raise ValueError
        elif flag.value_type == "number":
            values = [float(control_value), float(treatment_value)]
        else:
            values = [control_value, treatment_value]
    except ValueError:
        return _html_error(request, "error.html", "Variant values must match the flag type.")
    if any(not value_matches_type(value, flag.value_type) for value in values):
        return _html_error(request, "error.html", "Variant values must match the flag type.")
    setting = db.scalar(
        select(EnvironmentFlag).where(
            EnvironmentFlag.flag_id == flag.id,
            EnvironmentFlag.environment_id == environment.id,
        )
    )
    if setting is None:
        raise HTTPException(status_code=409, detail="Flag is not configured in this environment.")
    experiment = Experiment(
        project_id=project_id,
        flag_id=flag.id,
        environment_id=environment.id,
        key=key.strip(),
        primary_metric=primary_metric.strip(),
        variants=[
            {"key": "control", "value": values[0], "traffic_percentage": 50},
            {"key": "treatment", "value": values[1], "traffic_percentage": 50},
        ],
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
            changes={"key": {"old": None, "new": experiment.key}},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        return _html_error(request, "error.html", "Experiment key already exists.", 409)
    invalidate_project_config(db, project_id)
    return RedirectResponse(
        f"/projects/{project_id}/experiments",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/projects/{project_id}/sample-size", include_in_schema=False)
def sample_size_form(
    project_id: int,
    request: Request,
    baseline_rate: Annotated[float, Form()],
    minimum_detectable_effect: Annotated[float, Form()],
    power: Annotated[float, Form()],
    alpha: Annotated[float, Form()],
    csrf_token: Annotated[str, Form()],
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _verify_csrf(request, csrf_token)
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        inputs = SampleSizeInput(
            baseline_rate=baseline_rate,
            minimum_detectable_effect=minimum_detectable_effect,
            power=power,
            alpha=alpha,
        )
        users = sample_size_per_variant(
            inputs.baseline_rate,
            inputs.minimum_detectable_effect,
            inputs.power,
            inputs.alpha,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="sample_size_result.html",
            context={"error": str(exc)},
            status_code=422,
        )
    return templates.TemplateResponse(
        request=request,
        name="sample_size_result.html",
        context={"users_per_variant": users},
    )


@router.get(
    "/experiments/{experiment_id}/results",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def experiment_results_page(
    experiment_id: int,
    request: Request,
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    results = experiment_results(experiment_id, db, user)
    token = _csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="experiment_results.html",
        context={"user": user, "results": results, "csrf_token": token},
    )
    _set_csrf_cookie(response, token)
    return response


@router.post("/experiments/{experiment_id}/stop", include_in_schema=False)
def stop_experiment_form(
    experiment_id: int,
    request: Request,
    csrf_token: Annotated[str, Form()],
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> Response:
    _verify_csrf(request, csrf_token)
    experiment = db.scalar(
        select(Experiment)
        .join(Project)
        .where(Experiment.id == experiment_id, Project.owner_user_id == user.id)
    )
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    if experiment.status == "running":
        experiment.status = "completed"
        experiment.end_at = datetime.now(UTC)
        db.commit()
        invalidate_project_config(db, experiment.project_id)
    return RedirectResponse(
        f"/experiments/{experiment_id}/results",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    "/projects/{project_id}/audit-log",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def audit_log_page(
    project_id: int,
    request: Request,
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    project = db.scalar(
        select(Project).where(Project.id == project_id, Project.owner_user_id == user.id)
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    entries = list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.project_id == project_id)
            .order_by(AuditLog.created_at.desc())
            .limit(100)
        )
    )
    token = _csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="audit_log.html",
        context={"user": user, "project": project, "entries": entries, "csrf_token": token},
    )
    _set_csrf_cookie(response, token)
    return response


@router.get(
    "/projects/{project_id}/environments/{environment_id}/sdk-keys",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def sdk_keys_page(
    project_id: int,
    environment_id: int,
    request: Request,
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    environment = db.scalar(
        select(Environment)
        .join(Project)
        .where(
            Environment.id == environment_id,
            Environment.project_id == project_id,
            Project.owner_user_id == user.id,
        )
    )
    if environment is None:
        raise HTTPException(status_code=404, detail="Environment not found.")
    keys = list(
        db.scalars(
            select(SDKKey)
            .where(SDKKey.environment_id == environment_id)
            .order_by(SDKKey.created_at.desc())
        )
    )
    token = _csrf_token(request)
    response = templates.TemplateResponse(
        request=request,
        name="sdk_keys.html",
        context={
            "user": user,
            "project": environment.project,
            "environment": environment,
            "keys": keys,
            "csrf_token": token,
            "created_key": None,
        },
    )
    _set_csrf_cookie(response, token)
    return response


@router.post(
    "/projects/{project_id}/environments/{environment_id}/sdk-keys",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def create_sdk_key_form(
    project_id: int,
    environment_id: int,
    request: Request,
    name: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _verify_csrf(request, csrf_token)
    environment = db.scalar(
        select(Environment)
        .join(Project)
        .where(
            Environment.id == environment_id,
            Environment.project_id == project_id,
            Project.owner_user_id == user.id,
        )
    )
    if environment is None:
        raise HTTPException(status_code=404, detail="Environment not found.")
    secret = f"psk_{token_urlsafe(32)}"
    record = SDKKey(
        environment_id=environment.id,
        name=name.strip(),
        key_hash=_key_digest(secret),
    )
    db.add(record)
    db.commit()
    keys = list(db.scalars(select(SDKKey).where(SDKKey.environment_id == environment_id)))
    response = templates.TemplateResponse(
        request=request,
        name="sdk_keys.html",
        context={
            "user": user,
            "project": environment.project,
            "environment": environment,
            "keys": keys,
            "csrf_token": csrf_token,
            "created_key": secret,
        },
    )
    _set_csrf_cookie(response, csrf_token)
    return response


@router.post(
    "/projects/{project_id}/environments/{environment_id}/sdk-keys/{sdk_key_id}/revoke",
    include_in_schema=False,
)
def revoke_sdk_key_form(
    project_id: int,
    environment_id: int,
    sdk_key_id: int,
    request: Request,
    csrf_token: Annotated[str, Form()],
    user: User = Depends(get_dashboard_user),
    db: Session = Depends(get_db),
) -> Response:
    _verify_csrf(request, csrf_token)
    record = db.scalar(
        select(SDKKey)
        .join(Environment)
        .join(Project)
        .where(
            SDKKey.id == sdk_key_id,
            SDKKey.environment_id == environment_id,
            Environment.project_id == project_id,
            Project.owner_user_id == user.id,
        )
    )
    if record is None:
        raise HTTPException(status_code=404, detail="SDK key not found.")
    if record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        db.commit()
    return RedirectResponse(
        f"/projects/{project_id}/environments/{environment_id}/sdk-keys",
        status_code=status.HTTP_303_SEE_OTHER,
    )
