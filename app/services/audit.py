from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditLog


def record(db: Session, *, actor: str, action: str, entity_type: str,
           entity_id: str | None = None, summary: str = "",
           meta: dict | None = None, ip: str | None = None) -> None:
    db.add(AuditLog(actor=actor, action=str(action), entity_type=entity_type,
                    entity_id=str(entity_id) if entity_id else None,
                    summary=summary, meta=meta, ip=ip))
