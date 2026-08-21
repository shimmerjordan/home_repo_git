"""Inventory CRUD + lookup helpers."""
from __future__ import annotations

import json
from typing import Iterable

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import models


def location_path(loc: models.Location | None) -> str:
    if not loc:
        return ""
    parts: list[str] = []
    cur: models.Location | None = loc
    seen: set[int] = set()
    while cur and cur.id not in seen:
        parts.append(cur.name)
        seen.add(cur.id)
        cur = cur.parent
    return " / ".join(reversed(parts))


def serialize_item(item: models.Item) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "aliases": item.aliases or "",
        "category": item.category or "",
        "tags": item.tags or "",
        "quantity": item.quantity,
        "price": item.price,
        "note": item.note or "",
        "location_id": item.location_id,
        "pos_x": item.pos_x,
        "pos_z": item.pos_z,
        "location_path": location_path(item.location) if item.location else None,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _parse_geometry(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else None
    except (ValueError, TypeError):
        return None


def serialize_location(loc: models.Location) -> dict:
    return {
        "id": loc.id,
        "uuid": loc.uuid or "",
        "name": loc.name,
        "kind": loc.kind,
        "parent_id": loc.parent_id,
        "note": loc.note or "",
        "geometry": _parse_geometry(loc.geometry),
        "full_path": location_path(loc),
        "created_at": loc.created_at,
    }


# 只有这三种流水存得下"操作前的值", 才回滚得回去 (与 routers/revise.py 保持一致)。
UNDOABLE_ACTIONS = {"take_out", "put_in", "consume"}


def serialize_transaction(tx: models.Transaction) -> dict:
    return {
        "id": tx.id,
        "item_id": tx.item_id,
        "item_name": tx.item.name if tx.item else "",
        "action": tx.action,
        "quantity": tx.quantity,
        "location_id": tx.location_id,
        "location_path": location_path(tx.location) if tx.location else None,
        "note": tx.note or "",
        "created_at": tx.created_at,
        # 先给个保守默认。列表接口会用 annotate_undoable 批量覆盖成真实值。
        "undoable": False,
        "undo_note": "",
    }


def annotate_undoable(db: Session, rows: list[dict]) -> list[dict]:
    """给一批已序列化的流水标上"能不能回撤"。

    判据与 /api/revise/undo 的护栏完全一致:
      1. action 必须是 take_out / put_in / consume (adjust 没存操作前的数量);
      2. 必须是该物品**最后一条**流水 —— 否则按原数量反算会得到错误库存
         (中间可能有飞书机器人又存入过);
      3. 物品还在。

    第 2 条前端算不出来: 它只拿到最近 N 条, 看不到窗口外的更晚流水。
    这里用一条 GROUP BY 拿到每个物品的最后一条 id, 不做 N+1。
    """
    if not rows:
        return rows
    item_ids = {r["item_id"] for r in rows if r.get("item_id")}
    if not item_ids:
        return rows
    latest: dict[int, int] = dict(
        db.query(models.Transaction.item_id, func.max(models.Transaction.id))
        .filter(models.Transaction.item_id.in_(item_ids))
        .group_by(models.Transaction.item_id)
        .all()
    )
    alive = {
        i for (i,) in db.query(models.Item.id).filter(models.Item.id.in_(item_ids)).all()
    }
    for r in rows:
        if r["action"] not in UNDOABLE_ACTIONS:
            r["undoable"] = False
            r["undo_note"] = f"「{r['action']}」记录没保存操作前的数量, 无法回撤"
        elif r["item_id"] not in alive:
            r["undoable"] = False
            r["undo_note"] = "对应的物品已被删除, 无法回撤"
        elif latest.get(r["item_id"]) != r["id"]:
            r["undoable"] = False
            r["undo_note"] = f"「{r['item_name']}」在这之后又被动过, 无法安全回撤"
        else:
            r["undoable"] = True
            r["undo_note"] = ""
    return rows


# --- Search helpers (keyword-based pre-filter for LLM intent) ---

CN_PUNCT = "，。、；：！？“”‘’（）【】《》"


def _tokenize(text: str) -> list[str]:
    """Cheap tokenizer that works for mixed CN/EN: collapses whitespace, drops punctuation,
    and emits 2/3-char rolling shingles for CJK plus whole tokens for ASCII words."""
    text = (text or "").strip()
    for ch in CN_PUNCT:
        text = text.replace(ch, " ")
    tokens: list[str] = []
    buf_cjk: list[str] = []
    word: list[str] = []

    def flush_cjk():
        if buf_cjk:
            seg = "".join(buf_cjk)
            tokens.append(seg)
            for n in (2, 3):
                if len(seg) > n:
                    for i in range(len(seg) - n + 1):
                        tokens.append(seg[i : i + n])
            buf_cjk.clear()

    def flush_word():
        if word:
            tokens.append("".join(word).lower())
            word.clear()

    for ch in text:
        if "一" <= ch <= "鿿":
            flush_word()
            buf_cjk.append(ch)
        elif ch.isalnum():
            flush_cjk()
            word.append(ch)
        else:
            flush_cjk()
            flush_word()
    flush_cjk()
    flush_word()
    # Dedup, keep order.
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if len(t) >= 1 and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def search_items(db: Session, query: str, limit: int = 20,
                 include_depleted: bool = False) -> list[models.Item]:
    """Score items by token overlap against name/aliases/category/tags.

    `include_depleted=False` (default) hides items whose quantity has dropped to
    0 (i.e. "用完了"). The voice/intent path uses this default so "充电宝在哪"
    after the user said "我用完了充电宝" won't surface the depleted record.
    The "待补充" UI passes include_depleted=True via the dedicated endpoint.
    """
    tokens = _tokenize(query)
    if not tokens:
        q = db.query(models.Item)
        if not include_depleted:
            q = q.filter(models.Item.quantity > 0)
        return q.limit(limit).all()

    # Build OR-of-LIKE for each token so the DB pre-filters.
    conds = []
    for t in tokens:
        like = f"%{t}%"
        conds.append(models.Item.name.ilike(like))
        conds.append(models.Item.aliases.ilike(like))
        conds.append(models.Item.category.ilike(like))
        conds.append(models.Item.tags.ilike(like))
    base_query = db.query(models.Item).filter(or_(*conds))
    if not include_depleted:
        base_query = base_query.filter(models.Item.quantity > 0)
    rows: Iterable[models.Item] = base_query.all()

    def score(item: models.Item) -> float:
        haystack = " ".join([
            item.name or "",
            item.aliases or "",
            item.category or "",
            item.tags or "",
            item.note or "",
        ]).lower()
        s = 0.0
        for t in tokens:
            if not t:
                continue
            tl = t.lower()
            if tl in (item.name or "").lower():
                s += 3.0
            if tl in (item.aliases or "").lower():
                s += 2.5
            if tl in (item.category or "").lower():
                s += 1.2
            if tl in (item.tags or "").lower():
                s += 1.0
            if tl in haystack:
                s += 0.3
            # Length bonus to favor matches of longer tokens.
            s += min(len(t), 4) * 0.1
        return s

    scored = sorted(((score(i), i) for i in rows), key=lambda x: x[0], reverse=True)
    return [i for s, i in scored if s > 0][:limit]
