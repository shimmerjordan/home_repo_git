import csv
import io
import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services import audit
from ..services.inventory import (
    location_path,
    search_items,
    serialize_item,
    serialize_transaction,
)

router = APIRouter(prefix="/api/items", tags=["items"])

CSV_COLUMNS = [
    "name", "aliases", "category", "tags",
    "quantity", "price",
    "container_uuid", "container_path",
    "pos_x", "pos_z", "note",
]


def _build_csv(rows: list[list], db: Session, filename: str, *,
               include_template_hint: bool = False) -> StreamingResponse:
    """Always appends a `# 容器参考` section so users know what UUIDs to use.
    Lines starting with `#` are skipped on import."""
    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM (Excel-friendly)
    writer = csv.writer(buf)

    if include_template_hint:
        writer.writerow(["# 物品导入模板  ·  填好下方数据行后保存为 UTF-8 CSV 上传"])
        writer.writerow(["# container_uuid 为首选 (从下方参考表复制); 没有 UUID 时可填 container_path 如 客厅/柜子1"])
        writer.writerow(["# 以 # 开头的行会在导入时被忽略"])
        writer.writerow([])

    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow(r)

    # Container reference appended at the end (skipped on import).
    writer.writerow([])
    writer.writerow(["# === 容器参考 (导入忽略, 仅供复制 UUID) ==="])
    writer.writerow(["#uuid", "name", "path", "kind", "levels"])
    for loc in db.query(models.Location).order_by(models.Location.id).all():
        geo = {}
        try:
            geo = json.loads(loc.geometry) if loc.geometry else {}
        except Exception:
            geo = {}
        writer.writerow([
            "# " + (loc.uuid or ""),
            loc.name,
            location_path(loc),
            loc.kind,
            int(geo.get("levels") or 0),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _resolve_location(db: Session, path: str) -> int | None:
    """Walk a 'A / B / C' path, creating missing segments. Backward-compatible:
    - Legacy paths like "客厅 / 沙发" (without a home prefix) are still accepted.
      We resolve them by FIRST searching under the active default home, then
      falling back to direct top-level.
    - New paths like "我家 / 客厅 / 沙发" pass straight through.
    - Auto-created top segments default to kind='home' so a fresh import naturally
      builds the new hierarchy. Mid-segments default to 'room' (1st level under a
      home) or 'box' (deeper).
    """
    path = (path or "").strip()
    if not path:
        return None
    parts = [p.strip() for p in path.replace("／", "/").split("/") if p.strip()]
    if not parts:
        return None

    # Detect "legacy" path: first segment matches an existing non-home top-level
    # location. Prepend the default home so the chain stays valid post-migration.
    first_top = (
        db.query(models.Location)
        .filter(models.Location.name == parts[0], models.Location.parent_id == None)  # noqa: E711
        .first()
    )
    if first_top and first_top.kind != "home":
        # No home yet — fall through, the existing behaviour creates a 'room' at top.
        pass
    elif not first_top:
        # First segment doesn't exist at top. If a home exists and the SECOND-level
        # location with this name lives under that home, treat the path as legacy.
        any_home = db.query(models.Location).filter(models.Location.kind == "home").first()
        if any_home:
            under_home = (
                db.query(models.Location)
                .filter(models.Location.name == parts[0],
                        models.Location.parent_id == any_home.id)
                .first()
            )
            if under_home:
                parts = [any_home.name, *parts]

    parent_id: int | None = None
    last_id: int | None = None
    for depth, name in enumerate(parts):
        loc = (
            db.query(models.Location)
            .filter(models.Location.name == name, models.Location.parent_id == parent_id)
            .first()
        )
        if not loc:
            if depth == 0:
                kind = "home"
            elif depth == 1:
                kind = "room"
            else:
                kind = "box"
            loc = models.Location(name=name, parent_id=parent_id, kind=kind)
            db.add(loc)
            db.flush()
        parent_id = loc.id
        last_id = loc.id
    return last_id


# ---- IMPORTANT: All non-numeric paths MUST be declared BEFORE /{item_id:int}.
# FastAPI doesn't fall through on path-param validation; the first match returns 422.

@router.get("/export.csv")
def export_csv(db: Session = Depends(get_db)):
    items = db.query(models.Item).order_by(models.Item.id).all()
    rows = []
    for it in items:
        rows.append([
            it.name, it.aliases or "", it.category or "", it.tags or "",
            it.quantity or 0, it.price or 0,
            (it.location.uuid if it.location else ""),
            location_path(it.location) if it.location else "",
            "" if it.pos_x is None else it.pos_x,
            "" if it.pos_z is None else it.pos_z,
            it.note or "",
        ])
    return _build_csv(rows, db, "items.csv")


@router.get("/import-template.csv")
def import_template(db: Session = Depends(get_db)):
    # Pick first available container as a sample reference; otherwise use a placeholder.
    sample_uuid = ""
    sample_path = "客厅 / 柜子1"
    first = db.query(models.Location).first()
    if first:
        sample_uuid = first.uuid or ""
        sample_path = location_path(first)
    rows = [
        ["充电宝", "移动电源,power bank", "电子", "便携", 2, 99.0, sample_uuid, sample_path, "", "", "20000mAh"],
        ["螺丝刀套装", "改锥", "工具", "维修", 1, 45.0, "", sample_path, "", "", "含 6 个批头"],
    ]
    return _build_csv(rows, db, "items_template.csv", include_template_hint=True)


@router.post("/import")
async def import_csv(
    file: UploadFile = File(...),
    mode: str = Query("upsert", pattern="^(upsert|replace|append)$"),
    db: Session = Depends(get_db),
):
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    # Strip comment lines (starting with `#`) so the container-reference block is ignored.
    cleaned_lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")]
    cleaned_lines = [ln for ln in cleaned_lines if ln.strip()]
    if not cleaned_lines:
        raise HTTPException(400, "CSV has no data rows")
    reader = csv.DictReader(io.StringIO("\n".join(cleaned_lines)))
    if not reader.fieldnames:
        raise HTTPException(400, "CSV is empty or missing header")
    if "name" not in reader.fieldnames:
        raise HTTPException(400, "missing required column: name")

    created = updated = 0
    if mode == "replace":
        db.query(models.Transaction).delete()
        db.query(models.Item).delete()
        db.flush()

    for row in reader:
        name = (row.get("name") or "").strip()
        if not name:
            continue

        # Resolve container: prefer UUID, fall back to path.
        loc_id: int | None = None
        cu = (row.get("container_uuid") or "").strip()
        if cu:
            loc = db.query(models.Location).filter(models.Location.uuid == cu).first()
            if loc:
                loc_id = loc.id
        if loc_id is None:
            cp = (row.get("container_path") or row.get("location_path") or "").strip()
            if cp:
                loc_id = _resolve_location(db, cp)

        def _opt_float(v):
            v = (v or "").strip()
            try:
                return float(v) if v else None
            except ValueError:
                return None

        existing = None
        if mode == "upsert":
            existing = db.query(models.Item).filter(models.Item.name == name).first()

        payload = dict(
            name=name,
            aliases=(row.get("aliases") or "").strip(),
            category=(row.get("category") or "").strip(),
            tags=(row.get("tags") or "").strip(),
            quantity=int(float(row.get("quantity") or 0)) if (row.get("quantity") or "").strip() else 0,
            price=float(row.get("price") or 0) if (row.get("price") or "").strip() else 0.0,
            note=(row.get("note") or "").strip(),
            location_id=loc_id,
            pos_x=_opt_float(row.get("pos_x")),
            pos_z=_opt_float(row.get("pos_z")),
        )
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
            existing.updated_at = datetime.now()
            updated += 1
        else:
            db.add(models.Item(**payload))
            created += 1
    db.commit()
    return {"created": created, "updated": updated, "mode": mode}


# ---- Generic CRUD ----

@router.get("", response_model=list[schemas.ItemOut])
def list_items(
    q: str | None = Query(None),
    location_id: int | None = Query(None),
    category: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    # quantity == 0 = 用完/缺货, 默认对搜索和列表都隐藏(它们在 "待补充" 页面单独显示)。
    # 物品页 / CSV 导出等需要看全部的场景显式传 include_depleted=true。
    include_depleted: bool = Query(False),
    only_depleted: bool = Query(False),
    db: Session = Depends(get_db),
):
    if q:
        rows = search_items(db, q, limit=limit)
    else:
        query = db.query(models.Item)
        if location_id is not None:
            query = query.filter(models.Item.location_id == location_id)
        if category:
            query = query.filter(models.Item.category == category)
        rows = query.order_by(models.Item.updated_at.desc()).limit(limit).all()
    if only_depleted:
        rows = [r for r in rows if (r.quantity or 0) <= 0]
    elif not include_depleted:
        rows = [r for r in rows if (r.quantity or 0) > 0]
    return [serialize_item(r) for r in rows]


@router.get("/depleted")
def list_depleted(db: Session = Depends(get_db)):
    """物品消耗完(quantity ≤ 0)的 "待补充" 列表。

    与 pending-returns 是两类提醒:
      - 待补充: 用完了, 库存=0, 需要重新购入或决定弃用
      - 待归位: 借出未归位, 物理上还在外面
    UI 在待补充列表里把"已弃用 → 从数据库清除"的删除操作做掉。删除走标准
    DELETE /api/items/{id}, audit 会记录。
    """
    rows = (
        db.query(models.Item)
        .filter((models.Item.quantity == 0) | (models.Item.quantity.is_(None)))
        .order_by(models.Item.updated_at.desc())
        .all()
    )
    return [serialize_item(r) for r in rows]


@router.post("", response_model=schemas.ItemOut)
def create_item(payload: schemas.ItemCreate, db: Session = Depends(get_db)):
    item = models.Item(**payload.model_dump())
    db.add(item)
    db.flush()
    db.add(models.Transaction(
        item_id=item.id, action="put_in",
        quantity=item.quantity, location_id=item.location_id,
        note="手动创建",
    ))
    audit.log(db, "item", item.id, "create", name=item.name,
              after=serialize_item(item))
    db.commit()
    db.refresh(item)
    return serialize_item(item)


@router.get("/{item_id}", response_model=schemas.ItemOut)
def get_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(models.Item, item_id)
    if not item:
        raise HTTPException(404, "item not found")
    return serialize_item(item)


@router.patch("/{item_id}", response_model=schemas.ItemOut)
def update_item(item_id: int, payload: schemas.ItemUpdate, db: Session = Depends(get_db)):
    item = db.get(models.Item, item_id)
    if not item:
        raise HTTPException(404, "item not found")
    before = serialize_item(item)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(item, k, v)
    item.updated_at = datetime.now()
    db.flush()
    audit.log(db, "item", item.id, "update", name=item.name,
              before=before, after=serialize_item(item))
    db.commit()
    db.refresh(item)
    return serialize_item(item)


@router.delete("/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(models.Item, item_id)
    if not item:
        raise HTTPException(404, "item not found")
    before = serialize_item(item)
    name = item.name
    db.delete(item)
    db.flush()
    audit.log(db, "item", item_id, "delete", name=name, before=before)
    db.commit()
    return {"ok": True}


@router.post("/{item_id}/transactions", response_model=schemas.TransactionOut)
def record_transaction(
    item_id: int,
    payload: schemas.TransactionCreate,
    db: Session = Depends(get_db),
):
    item = db.get(models.Item, item_id)
    if not item:
        raise HTTPException(404, "item not found")
    if payload.item_id != item_id:
        raise HTTPException(400, "item_id mismatch")
    qty = payload.quantity
    if payload.action == "take_out":
        item.quantity = max(0, (item.quantity or 0) - qty)
    elif payload.action == "put_in":
        item.quantity = (item.quantity or 0) + qty
        if payload.location_id:
            item.location_id = payload.location_id
    elif payload.action == "adjust":
        item.quantity = qty
    item.updated_at = datetime.now()
    tx = models.Transaction(**payload.model_dump())
    db.add(tx)
    db.flush()
    audit.log(db, "transaction", tx.id, payload.action,
              name=item.name,
              after={"action": payload.action, "qty": payload.quantity,
                     "remaining": item.quantity, "note": payload.note or ""})
    db.commit()
    db.refresh(tx)
    return serialize_transaction(tx)


@router.get("/{item_id}/transactions", response_model=list[schemas.TransactionOut])
def list_transactions(item_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(models.Transaction)
        .filter(models.Transaction.item_id == item_id)
        .order_by(models.Transaction.created_at.desc())
        .all()
    )
    return [serialize_transaction(r) for r in rows]


# Global recent transactions feed.
recent_router = APIRouter(prefix="/api/transactions", tags=["transactions"])


@recent_router.get("", response_model=list[schemas.TransactionOut])
def recent_transactions(
    limit: int = Query(50, ge=1, le=2000),
    q: str | None = Query(None, description="按物品名搜索"),
    action: str | None = Query(None, pattern="^(take_out|put_in|adjust|consume)$"),
    location_id: int | None = Query(None),
    item_id: int | None = Query(None),
    since: str | None = Query(None, description="ISO 时间, 含此时刻之后"),
    until: str | None = Query(None, description="ISO 时间, 截止此时刻"),
    db: Session = Depends(get_db),
):
    query = db.query(models.Transaction).join(
        models.Item, models.Transaction.item_id == models.Item.id, isouter=True
    )
    if action:
        query = query.filter(models.Transaction.action == action)
    if item_id:
        query = query.filter(models.Transaction.item_id == item_id)
    if location_id:
        query = query.filter(models.Transaction.location_id == location_id)
    if q:
        like = f"%{q}%"
        query = query.filter(models.Item.name.ilike(like))
    if since:
        try:
            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            query = query.filter(models.Transaction.created_at >= dt)
        except ValueError:
            pass
    if until:
        try:
            dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
            query = query.filter(models.Transaction.created_at <= dt)
        except ValueError:
            pass
    rows = query.order_by(models.Transaction.created_at.desc()).limit(limit).all()
    return [serialize_transaction(r) for r in rows]


@recent_router.get("/pending-returns")
def pending_returns(db: Session = Depends(get_db)):
    """List items currently checked out and awaiting return (借出未归位).

    Definition: per-item, sum(take_out qty) - sum(put_in qty) - sum(consume qty)
    > 0. We compute this by walking ALL transactions in chronological order and
    keeping a running balance; the per-item residual is the pending-return qty.
    For each item we also surface the LAST take_out so the UI can show how long
    it's been out.
    """
    pending: dict[int, dict] = {}
    rows = (
        db.query(models.Transaction)
        .order_by(models.Transaction.created_at.asc())
        .all()
    )
    for tx in rows:
        slot = pending.setdefault(tx.item_id, {"qty": 0, "last_take": None, "last_take_loc": None})
        if tx.action == "take_out":
            slot["qty"] += tx.quantity
            slot["last_take"] = tx.created_at
            slot["last_take_loc"] = tx.location_id
        elif tx.action in ("put_in", "consume"):
            slot["qty"] = max(0, slot["qty"] - tx.quantity)
            if slot["qty"] == 0:
                slot["last_take"] = None
                slot["last_take_loc"] = None
        # adjust: ignored — it's a manual recount, not borrow/return
    out = []
    for item_id, slot in pending.items():
        if slot["qty"] <= 0:
            continue
        item = db.query(models.Item).get(item_id)
        if not item:
            continue
        # location to RETURN to (where it was when taken out).
        ret_loc = None
        if slot["last_take_loc"]:
            ret_loc = db.query(models.Location).get(slot["last_take_loc"])
        out.append({
            "item_id": item.id,
            "item_name": item.name,
            "pending_quantity": slot["qty"],
            "last_take_at": slot["last_take"].isoformat() if slot["last_take"] else None,
            "return_location_id": ret_loc.id if ret_loc else None,
            "return_location_path": location_path(ret_loc) if ret_loc else None,
        })
    out.sort(key=lambda r: r["last_take_at"] or "", reverse=True)
    return out
