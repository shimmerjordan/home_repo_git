"""Inventory CRUD + lookup helpers."""
from __future__ import annotations

import json
from typing import Iterable

from sqlalchemy import or_
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
    }


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


def search_items(db: Session, query: str, limit: int = 20) -> list[models.Item]:
    """Score items by token overlap against name/aliases/category/tags."""
    tokens = _tokenize(query)
    if not tokens:
        return db.query(models.Item).limit(limit).all()

    # Build OR-of-LIKE for each token so the DB pre-filters.
    conds = []
    for t in tokens:
        like = f"%{t}%"
        conds.append(models.Item.name.ilike(like))
        conds.append(models.Item.aliases.ilike(like))
        conds.append(models.Item.category.ilike(like))
        conds.append(models.Item.tags.ilike(like))
    rows: Iterable[models.Item] = db.query(models.Item).filter(or_(*conds)).all()

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
