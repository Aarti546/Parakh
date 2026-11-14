from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog


def record_change(
    db: Session,
    *,
    project_id: int,
    actor_user_id: int,
    entity_type: str,
    entity_id: int,
    action: str,
    changes: dict[str, Any],
) -> None:
    db.add(
        AuditLog(
            project_id=project_id,
            actor_user_id=actor_user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            changes=changes,
        )
    )
