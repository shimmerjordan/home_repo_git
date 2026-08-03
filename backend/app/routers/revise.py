"""操作复核与改判 — 撤销一条流水, 或撤销后改到正确的目标上重做。

为什么需要它: 语音"存入充电宝"时后端会模糊匹配库存, 匹配错了就把库存加到了别人头上。
改判 = 硬回滚那条流水 + 对正确目标重新执行, **必须在同一个数据库事务里完成** ——
拆成前端两次调用的话, 中间失败会留下"撤销了但没重执行"的破损状态。

撤销语义是硬回滚(当作没发生), 不是反向补偿流水: 用户是来纠错的, 错误不该留在流水里。
撤销行为本身写进审计日志, 可追溯性不丢。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services import audit
from ..services.inventory import location_path, serialize_item

router = APIRouter(prefix="/api/revise", tags=["revise"])

# adjust 存不下"操作前的值", 回滚会算错, 直接拒绝。
UNDOABLE_ACTIONS = {"take_out", "put_in", "consume"}


class UndoBody(BaseModel):
    transaction_id: int


class RedirectTarget(BaseModel):
    item_id: int | None = None
    new_item_name: str | None = None


class RedirectBody(BaseModel):
    transaction_id: int
    target: RedirectTarget


def _load_undoable(db: Session, transaction_id: int) -> tuple[models.Transaction, models.Item]:
    """取出可撤销的流水 + 它的物品, 不满足条件直接 400。"""
    tx = db.get(models.Transaction, transaction_id)
    if not tx:
        raise HTTPException(404, "这条操作记录不存在, 可能已经被撤销过了")
    if tx.action not in UNDOABLE_ACTIONS:
        raise HTTPException(
            400, f"「{tx.action}」类型的记录无法撤销: 流水里没有保存操作前的数量"
        )
    item = db.get(models.Item, tx.item_id)
    if not item:
        raise HTTPException(400, "这条记录对应的物品已被删除, 无法撤销")

    # 护栏: 只能撤销该物品的最后一条流水。
    # 不是并发检测, 而是保证回滚本身有意义 —— 若中间飞书机器人又存入过,
    # 按原数量反算会得到错误的库存。
    latest = (
        db.query(models.Transaction)
        .filter(models.Transaction.item_id == tx.item_id)
        .order_by(models.Transaction.created_at.desc(), models.Transaction.id.desc())
        .first()
    )
    if latest and latest.id != tx.id:
        raise HTTPException(
            400, f"「{item.name}」在这次操作之后又被动过, 无法安全撤销。请到物品页手工调整"
        )
    return tx, item


def _rollback_tx(db: Session, tx: models.Transaction, item: models.Item) -> str:
    """硬回滚一条流水。返回一句人话描述。不 commit。"""
    qty = tx.quantity or 0
    before = serialize_item(item)

    if tx.action == "put_in":
        # 若这条就是该物品最早的流水, 说明物品是这次操作建出来的 —— 连物品一起删。
        earliest = (
            db.query(models.Transaction)
            .filter(models.Transaction.item_id == item.id)
            .order_by(models.Transaction.created_at.asc(), models.Transaction.id.asc())
            .first()
        )
        if earliest and earliest.id == tx.id:
            name = item.name
            db.delete(item)          # cascade 会一并删掉这条流水
            db.flush()
            audit.log(db, "item", before["id"], "delete", name=name,
                      before=before, after={"reason": "撤销新建"})
            return f"已撤销新建「{name}」, 物品记录一并移除"
        item.quantity = max(0, (item.quantity or 0) - qty)
    else:  # take_out / consume —— 当初减掉的加回来
        item.quantity = (item.quantity or 0) + qty

    item.updated_at = datetime.now()
    db.delete(tx)
    db.flush()
    audit.log(db, "item", item.id, "update", name=item.name,
              before=before, after=serialize_item(item))
    return f"已撤销, 「{item.name}」现在共 {item.quantity} 件"


@router.post("/undo")
def undo(body: UndoBody, db: Session = Depends(get_db)):
    tx, item = _load_undoable(db, body.transaction_id)
    message = _rollback_tx(db, tx, item)
    db.commit()
    return {"ok": True, "message": message}


@router.post("/redirect")
def redirect(body: RedirectBody, db: Session = Depends(get_db)):
    """撤销原操作, 再把同样的动作施加到正确的目标上 —— 单事务, 要么全成要么全不动。"""
    if not body.target.item_id and not (body.target.new_item_name or "").strip():
        raise HTTPException(400, "请指定改判目标: 已有物品 id 或新物品名称")

    tx, item = _load_undoable(db, body.transaction_id)
    action = tx.action
    qty = tx.quantity or 0
    loc_id = tx.location_id
    origin_name = item.name

    if body.target.item_id and body.target.item_id == item.id:
        raise HTTPException(400, "改判目标和原目标是同一个物品")

    _rollback_tx(db, tx, item)

    # 解析新目标 —— 已有物品, 或按名字新建一个。
    if body.target.item_id:
        target_item = db.get(models.Item, body.target.item_id)
        if not target_item:
            raise HTTPException(404, "改判目标物品不存在")
        created = False
    else:
        name = body.target.new_item_name.strip()
        target_item = models.Item(name=name, location_id=loc_id, quantity=0)
        db.add(target_item)
        db.flush()
        audit.log(db, "item", target_item.id, "create", name=name,
                  after=serialize_item(target_item))
        created = True

    # 重新施加同一个动作。语义与 llm/intent._apply_stock_op 保持一致。
    if action in ("take_out", "consume"):
        target_item.quantity = max(0, (target_item.quantity or 0) - qty)
    else:  # put_in
        target_item.quantity = (target_item.quantity or 0) + qty
        if loc_id:
            target_item.location_id = loc_id
    target_item.updated_at = datetime.now()

    new_tx = models.Transaction(
        item_id=target_item.id,
        action=action,
        quantity=qty,
        location_id=loc_id or target_item.location_id,
        note="改判自「%s」" % origin_name,
    )
    db.add(new_tx)
    db.flush()
    db.commit()
    db.refresh(new_tx)
    db.refresh(target_item)

    loc_text = location_path(target_item.location) if target_item.location else "未指定位置"
    verb = {"take_out": "取出", "put_in": "存入", "consume": "用完"}[action]
    prefix = "已新建并" if created else "已改为"
    return {
        "ok": True,
        "message": f"{prefix}{verb}「{target_item.name}」×{qty} ({loc_text}), 共 {target_item.quantity} 件",
        "item_id": target_item.id,
        "transaction_id": new_tx.id,
        "created": created,
    }
