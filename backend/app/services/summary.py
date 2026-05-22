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
# When a need/question keyword is detected (e.g. "我发烧了", "有什么药吗") we widen the
# context to ALL items (capped) so the LLM can reason semantically over the catalogue.
NEED_KEYWORDS = ("需要", "有什么", "怎么", "推荐", "应该", "可以用", "可以吃", "可以治", "解决", "对付", "缓解", "舒缓", "止")
QUESTION_HINTS = ("吗", "?", "？", "呢")
ALL_ITEMS_CAP = 80


def _looks_like_need(text: str) -> bool:
    if not text:
        return False
    if any(k in text for k in NEED_KEYWORDS):
        return True
    # Question that's not "在哪/哪里" (those are clearly lookups).
    if any(h in text for h in QUESTION_HINTS) and not ("在哪" in text or "哪里" in text):
        return True
    return False


def build_summary(db: Session, query: str, *, fast_mode: bool = False) -> dict:
    """Return a structured summary string for the LLM."""
    prefilter_cap = 12 if fast_mode else MAX_PREFILTER
    # Top-N items most relevant to the query (keyword pre-filter).
    candidates = search_items(db, query, limit=prefilter_cap)
    need_mode = _looks_like_need(query)

    # Overview: category histogram + total counts. Depleted items (quantity=0)
    # are EXCLUDED — they're in the "待补充" list and shouldn't be suggested as
    # find/take/assist candidates by the LLM.
    all_items: list[models.Item] = (
        db.query(models.Item).filter(models.Item.quantity > 0).all()
    )
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
    if need_mode:
        # Full catalogue (capped) so the LLM can semantically pick relevant items for needs
        # like "我发烧了有什么药" — keyword search alone misses these.
        lines.append("")
        lines.append(f"完整库存清单 (最多 {ALL_ITEMS_CAP} 件, 用于需求推荐):")
        cand_ids = {it.id for it in candidates}
        extras = [it for it in all_items if it.id not in cand_ids][: ALL_ITEMS_CAP - len(candidates)]
        for it in candidates + extras:
            lines.append(
                f"  - id={it.id} 名={it.name} 类={it.category or '-'} 量={it.quantity} "
                f"位={location_path(it.location) if it.location else '-'}"
            )
    if recent_tx and not fast_mode:
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
