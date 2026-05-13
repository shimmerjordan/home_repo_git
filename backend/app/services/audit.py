"""Audit trail helper.

Every mutation to locations/items/transactions records an `AuditLog` row via
`audit.log(...)`. Diff is captured as `{field: [old, new]}` so the UI can render
git-style blame entries. The hook is `caller-driven` (explicit) rather than ORM
events so we keep full control over snapshot timing.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from .. import models


# Geometry diff is noisy when serialised whole. Compute it field-by-field so the
# audit shows e.g. "w: 0.5 → 0.6" not the full nested blob.
_GEOM_FIELDS = ("x", "y", "z", "w", "h", "d", "rot", "color", "levels", "level", "slot", "polygon")


def _flatten_geometry(g: Any) -> dict:
    if not isinstance(g, dict):
        return {}
    return {f"geometry.{k}": g.get(k) for k in _GEOM_FIELDS if k in g}


def diff(before: dict | None, after: dict | None) -> dict:
    """Return a {field: [old, new]} dict for fields that actually changed.
    Geometry is flattened to e.g. `geometry.w` so each numeric change is its own row."""
    if before is None and after is None:
        return {}
    if before is None:
        out = {"_created": True}
        if isinstance(after, dict):
            out.update({k: [None, v] for k, v in after.items() if k != "geometry"})
            out.update({k: [None, v] for k, v in _flatten_geometry(after.get("geometry")).items()})
        return out
    if after is None:
        out = {"_deleted": True}
        if isinstance(before, dict):
            out.update({k: [v, None] for k, v in before.items() if k != "geometry"})
            out.update({k: [v, None] for k, v in _flatten_geometry(before.get("geometry")).items()})
        return out
    changes: dict = {}
    keys = (set(before.keys()) | set(after.keys())) - {"geometry"}
    for k in keys:
        ov, nv = before.get(k), after.get(k)
        if ov != nv:
            changes[k] = [ov, nv]
    gb = _flatten_geometry(before.get("geometry"))
    ga = _flatten_geometry(after.get("geometry"))
    for k in set(gb.keys()) | set(ga.keys()):
        ov, nv = gb.get(k), ga.get(k)
        if ov != nv:
            changes[k] = [ov, nv]
    return changes


_ACTION_VERB = {"create": "新建", "update": "修改", "delete": "删除", "restore": "撤销恢复"}
_ENTITY_LABEL = {"location": "位置", "item": "物品", "transaction": "流水"}


def _build_summary(entity_type: str, action: str, name: str, changes: dict) -> str:
    verb = _ACTION_VERB.get(action, action)
    label = _ENTITY_LABEL.get(entity_type, entity_type)
    base = f"{verb}{label} 「{name}」" if name else f"{verb}{label}"
    if action == "update":
        # List up to 4 changed field names for a quick scan.
        keys = [k for k in changes.keys() if not k.startswith("_")]
        if keys:
            shown = keys[:4]
            more = "" if len(keys) <= 4 else f" 等 {len(keys)} 项"
            base += ": " + "/".join(shown) + more
    return base


def log(
    db: Session,
    entity_type: str,
    entity_id: int | None,
    action: str,
    *,
    name: str = "",
    before: dict | None = None,
    after: dict | None = None,
    source: str = "ui",
) -> None:
    """Insert one audit row. Commits the row immediately so it survives even if
    the surrounding business-logic transaction later fails (we'd rather log a
    spurious attempt than miss a real change)."""
    changes = diff(before, after)
    if not changes and action == "update":
        return  # nothing actually changed; skip noise
    entry = models.AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=(name or "")[:200],
        action=action,
        changes=json.dumps(changes, ensure_ascii=False, default=str),
        summary=_build_summary(entity_type, action, name, changes),
        source=source,
    )
    db.add(entry)
    # Flush so the row gets an id; callers commit the full DB transaction.
    db.flush()


def serialize(entry: models.AuditLog) -> dict:
    try:
        ch = json.loads(entry.changes) if entry.changes else {}
    except (ValueError, TypeError):
        ch = {}
    return {
        "id": entry.id,
        "ts": entry.ts,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "entity_name": entry.entity_name or "",
        "action": entry.action,
        "changes": ch,
        "summary": entry.summary or "",
        "source": entry.source or "",
    }
