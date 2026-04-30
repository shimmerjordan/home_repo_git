"""Build a compact inventory summary that fits in an LLM prompt without leaking the entire DB."""
from __future__ import annotations

from collections import Counter
from typing import Iterable

from sqlalchemy.orm import Session

from .. import models
from .inventory import location_path, search_items


MAX_PREFILTER = 30
MAX_OVERVIEW_CATS = 12
MAX_RECENT = 8


def build_summary(db: Session, query: str) -> dict:
    """Return a structured summary string for the LLM."""
    # Top-N items most relevant to the query (keyword pre-filter).
    candidates = search_items(db, query, limit=MAX_PREFILTER)

    # Overview: category histogram + total counts.
    all_items: list[models.Item] = db.query(models.Item).all()
    cat_counter: Counter[str] = Counter()
    for it in all_items:
        cat_counter[it.category or "未分类"] += 1
    top_cats = cat_counter.most_common(MAX_OVERVIEW_CATS)

    # Locations.
    locations = db.query(models.Location).all()

    # Recent transactions.
    recent_tx = (
        db.query(models.Transaction)
        .order_by(models.Transaction.created_at.desc())
        .limit(MAX_RECENT)
        .all()
    )

    # Render.
    lines: list[str] = []
    lines.append(f"共 {len(all_items)} 件物品。分类分布: " + ", ".join(
        f"{c}({n})" for c, n in top_cats
    ))
    lines.append("位置列表 (id|路径|类型):")
    for loc in locations:
        lines.append(f"  - {loc.id} | {location_path(loc)} | {loc.kind}")
    lines.append("")
    lines.append(f"与查询相关的候选物品 (Top {len(candidates)}, 按相关度排序):")
    if candidates:
        for it in candidates:
            lines.append(
                f"  - id={it.id} 名称={it.name!r} 别名={(it.aliases or '')!r} "
                f"分类={it.category!r} 数量={it.quantity} "
                f"位置={location_path(it.location) if it.location else '未指定'!r}"
            )
    else:
        lines.append("  (无关键词匹配，可能是新增或表达不准确)")
    if recent_tx:
        lines.append("")
        lines.append("近期记录:")
        for tx in recent_tx:
            iname = tx.item.name if tx.item else f"#{tx.item_id}"
            lines.append(
                f"  - {tx.created_at:%Y-%m-%d %H:%M} {tx.action} "
                f"{iname} x{tx.quantity}"
            )

    return {
        "text": "\n".join(lines),
        "candidates": [
            {
                "id": it.id,
                "name": it.name,
                "location_path": location_path(it.location) if it.location else None,
            }
            for it in candidates
        ],
        "locations": [
            {"id": loc.id, "path": location_path(loc)} for loc in locations
        ],
    }
