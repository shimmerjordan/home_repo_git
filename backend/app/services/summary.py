"""Build a compact inventory summary that fits in an LLM prompt without leaking the entire DB."""
from __future__ import annotations

import re
from collections import Counter

from sqlalchemy.orm import Session

from .. import models
from .inventory import location_path, search_items


MAX_PREFILTER = 30
MAX_OVERVIEW_CATS = 12
MAX_RECENT = 8
# 多物品语句里每个片段单独检索时各自取几条。
PER_SEGMENT_LIMIT = 6
# 分段后的候选总量上限 (防止 "把A、B、C、D、E、F、G放进X" 把 prompt 撑爆)。
MAX_SEGMENT_CANDIDATES = 40
# 库存为 0 的物品最多列几件 (只为让"再买了两瓶水"能命中已有档案而不是又建一行)。
MAX_DEPLETED = 15
# When a need/question keyword is detected (e.g. "我发烧了", "有什么药吗") we widen the
# context to ALL items (capped) so the LLM can reason semantically over the catalogue.
NEED_KEYWORDS = ("需要", "有什么", "怎么", "推荐", "应该", "可以用", "可以吃", "可以治", "解决", "对付", "缓解", "舒缓", "止")
QUESTION_HINTS = ("吗", "?", "？", "呢")
ALL_ITEMS_CAP = 80


# 把"我用完了一瓶洗手液, 拿了螺丝刀和卷尺, 顺便把两个充电器放回书桌1"切成若干片段。
# 为什么必须切: search_items 拿**整句**去检索, 各物品在 Top-N 里互相挤名额 ——
# 库存一多就会有物品根本进不了候选, LLM 拿不到它的 id, 只能瞎猜或留空,
# 于是后端退化成模糊匹配 top-1, 存错人头上。分段检索保证句子里提到的每个物品
# 都有自己的候选进 prompt。
_SEGMENT_SPLIT = re.compile(
    r"[、,，;；]|和(?![的了])|与(?![的了])|以及|还有|顺便|然后|再把|再帮我|同时"
)
# 动词/量词类噪声词, 单独成段时没有检索价值。
_SEGMENT_NOISE = {
    "我", "把", "帮我", "拿了", "拿出", "拿走", "取出", "借走", "借出", "放进", "放回", "放到",
    "存入", "存进", "用完了", "用完", "喝完了", "喝完", "吃了", "扔了", "丢了", "送人了",
    "新增", "新建", "添加", "录入", "删除", "删掉", "在哪", "哪里", "查一下", "找一下",
}


def split_segments(text: str) -> list[str]:
    """按并列连接词切分, 返回去噪去重后的片段列表 (原句总是第一个元素)。"""
    text = (text or "").strip()
    if not text:
        return []
    out: list[str] = [text]
    seen = {text}
    for raw in _SEGMENT_SPLIT.split(text):
        seg = (raw or "").strip(" 　的了呢吧啊")
        if not seg or len(seg) < 2 or seg in _SEGMENT_NOISE or seg in seen:
            continue
        seen.add(seg)
        out.append(seg)
    return out


def _segmented_candidates(db: Session, query: str, whole_limit: int) -> list[models.Item]:
    """整句检索 + 每个片段单独检索, 合并去重, 保持"整句相关度优先"的顺序。"""
    segments = split_segments(query)
    merged: list[models.Item] = []
    seen_ids: set[int] = set()
    for idx, seg in enumerate(segments):
        limit = whole_limit if idx == 0 else PER_SEGMENT_LIMIT
        for it in search_items(db, seg, limit=limit):
            if it.id in seen_ids:
                continue
            seen_ids.add(it.id)
            merged.append(it)
            if len(merged) >= MAX_SEGMENT_CANDIDATES:
                return merged
    return merged


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
    # 整句 + 逐片段检索, 保证多物品语句里每个物品都有候选进 prompt。
    candidates = _segmented_candidates(db, query, prefilter_cap)
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

    # 库存为 0 的档案。它们对 find/assist 是**不可见**的(在"待补充"列表里等处理),
    # 但对 put_in 必须可见 —— 否则"再买了两瓶水"会新建一条重复的"水", 而不是把
    # 已有那条补回来。所以单开一段并写清用途, 让 LLM 只在补货场景引用它。
    depleted = (
        db.query(models.Item)
        .filter(models.Item.quantity <= 0)
        .order_by(models.Item.updated_at.desc())
        .limit(MAX_DEPLETED)
        .all()
    )
    if depleted:
        lines.append("")
        lines.append("库存为0的旧档案 (仅在用户说'补货/又买了/再买了'时可作为 put_in 目标, "
                     "查找和推荐**不要**引用):")
        for it in depleted:
            lines.append(
                f"  - id={it.id} 名称={it.name!r} 别名={(it.aliases or '')!r} "
                f"位置={location_path(it.location) if it.location else '未指定'!r}"
            )

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
