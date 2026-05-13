from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services.audit import serialize

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
def list_audit(
    limit: int = Query(200, ge=1, le=2000),
    entity_type: str | None = Query(None, pattern="^(location|item|transaction)$"),
    action: str | None = Query(None, pattern="^(create|update|delete|restore)$"),
    entity_id: int | None = Query(None),
    q: str | None = Query(None, description="按 entity_name 或 summary 模糊搜索"),
    since: str | None = Query(None, description="ISO 时间, 含此时刻之后"),
    until: str | None = Query(None, description="ISO 时间, 截止此时刻"),
    db: Session = Depends(get_db),
):
    query = db.query(models.AuditLog)
    if entity_type:
        query = query.filter(models.AuditLog.entity_type == entity_type)
    if action:
        query = query.filter(models.AuditLog.action == action)
    if entity_id is not None:
        query = query.filter(models.AuditLog.entity_id == entity_id)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (models.AuditLog.entity_name.ilike(like)) | (models.AuditLog.summary.ilike(like))
        )
    if since:
        try:
            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            query = query.filter(models.AuditLog.ts >= dt)
        except ValueError:
            pass
    if until:
        try:
            dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            query = query.filter(models.AuditLog.ts <= dt)
        except ValueError:
            pass
    rows = query.order_by(models.AuditLog.ts.desc()).limit(limit).all()
    return [serialize(r) for r in rows]
