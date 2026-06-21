"""Voice-text → structured intent → DB action.

Pipeline:
  1. Build a compact inventory summary scoped to the query.
  2. Ask the LLM (via OpenAI-compatible chat) to choose an intent + parameters.
     Prefer tool-calling; fall back to JSON mode for providers that don't support tools.
  3. Validate the response, compute confidence, optionally execute.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from ..config import AppConfig
from ..services.inventory import location_path, search_items, serialize_transaction
from ..services.summary import build_summary
from .client import LLMClient, LLMError


SYSTEM_PROMPT = """你是家庭仓储管家的语义解析器, 同时要给出温暖、口语化的中文回答。
你的工作:
1. 阅读用户语句和当前的库存摘要
2. 注意位置层级最上层可能是"家"(如 我家 / 老家 / 父母家),"家"下面才是房间。用户没特别说"在老家"之类的话, 默认指主要居住的家
3. 选择一个意图: find / take_out / put_in / consume / list / create_item / assist / unknown
   - take_out: "借出" — 我拿出来用一下, **稍后需要归位**(如 "我拿了卷尺"/"借走螺丝刀"/"拿出充电宝")
   - consume:  "消耗完" — 用完了/扔了/吃了/送人了, **不会归位**(如 "我喝完了一瓶水"/"用完最后一支牙膏"/"吃了两片药"/"扔了过期面包")
   - 如果用户没明示但语义模糊(如"拿了 X"), 默认 take_out (借出, 提示归位); "用了/喝了/吃了/扔了/丢了/送了" 这类完成态明显是 consume
   - assist: 用户表达"需求/症状/问题"(如 "我发烧了家里有什么药"、"想擦地板用什么"),
     需要你从库存里挑选可能解决该需求的所有相关物品。把 item_id 全部放进 candidates,
     并在 recommendations 里给出 [{item_id, purpose}] 说明每件物品的用途, speech 用一句
     人话总结(如"家里有这几样可以试: 布洛芬退烧 / 体温计 / 维C")
4. 在候选物品中匹配用户最可能指的物品(基于名称/别名/分类/上下文做语义匹配, 如果用户说"在老家", 优先匹配老家下面的物品)
5. 给出 0~1 之间的 confidence:
   - 物品名/位置和用户说法明确一致 -> 0.9+
   - 通过别名/语义推断 -> 0.6~0.85
   - 多个候选难以区分或缺关键信息 -> <0.5
   - 库存里没找到匹配 -> <0.3
6. speech 字段: 用一句温暖的中文话术回答用户(不超过60字), 风格参考下面的例子, 适当口语化:
   - 找到: "找到啦, 充电宝在卧室床头柜, 库存 1 个"
   - 模糊找到: "你可能想找的是充电宝, 放在卧室床头柜哦"
   - 没找到: "暂未找到这种东西, 可能还没登记进来"
   - 借出 (take_out): "已记录取出充电宝 1 个, 用完记得归位哦"
   - 消耗 (consume): "已记录用完 1 瓶水, 剩 2 瓶"
   - 存入成功: "好的, 已存入螺丝刀到工具箱, 现在共 3 件"
   - 新增成功: "记下啦, 充电宝放在卧室床头柜了"
   - 不确定: "我不太确定, 是想找充电宝吗"

注意:
- "我刚拿了X"对应 take_out (借出, 待归位); "我用完了X" / "X 喝完了" / "扔了X" 对应 consume (永久减库存, 不待归位)
- "我把X放在Y了"对应 put_in (归位; 如果有 take_out 待归位的同名物品, 自动抵消)
- 找不到精确匹配, 请在 candidates 列出最接近的几个 id, 并把 confidence 调低
- 用户明确说"创建/新增/添加/记录一个新物品"时, 必须使用 create_item, 不要改成 put_in。合并判断由后端负责, 你只需正确识别意图和提取 item_name
- create_item 必须包含 item_name 和(可选) location_id
- 如果完全无法理解, intent=unknown, confidence=0
"""


INTENT_SCHEMA_HINT = """{
  "intent": "find|take_out|put_in|consume|list|create_item|assist|unknown",
  "confidence": 0.0,
  "speech": "string (中文, 给用户的简短回答)",
  "item_id": null,                // 已存在物品 id (find/take_out/put_in)
  "item_name": null,              // create_item 用
  "location_id": null,            // put_in / create_item 可用
  "quantity": 1,
  "candidates": [123, 456],       // 备选 item_id, 当不确定时给出
  "reasoning": "string (一句解释)"
}"""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_intent",
            "description": "Submit the parsed intent for the user's voice query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "enum": ["find", "take_out", "put_in", "consume", "list", "create_item", "assist", "unknown"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "speech": {"type": "string"},
                    "item_id": {"type": ["integer", "null"]},
                    "item_name": {"type": ["string", "null"]},
                    "location_id": {"type": ["integer", "null"]},
                    "quantity": {"type": "integer", "default": 1},
                    "candidates": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "default": [],
                    },
                    "recommendations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item_id": {"type": "integer"},
                                "purpose": {"type": "string"},
                            },
                            "required": ["item_id", "purpose"],
                        },
                        "default": [],
                    },
                    "reasoning": {"type": "string"},
                },
                "required": ["intent", "confidence", "speech"],
            },
        },
    }
]


async def parse_intent(text: str, db: Session, cfg: AppConfig) -> dict[str, Any]:
    summary = build_summary(db, text, fast_mode=cfg.llm.fast_mode)

    user_msg = (
        f"用户语句: {text}\n\n"
        f"当前库存摘要:\n{summary['text']}\n\n"
        f"请基于以上摘要和用户语句解析意图。"
    )
    if cfg.llm.supports_tools:
        user_msg += "\n请调用 submit_intent 工具返回结果。"
    else:
        user_msg += f"\n请按以下 JSON schema 返回:\n{INTENT_SCHEMA_HINT}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    client = LLMClient(cfg.llm)
    parsed: dict[str, Any]
    if cfg.llm.supports_tools:
        result = await client.chat(messages, tools=TOOLS)
        if result["tool_calls"]:
            parsed = result["tool_calls"][0]["arguments"]
        elif result["content"]:
            # Fallback if model ignored the tool.
            parsed = await client.chat_json(messages, schema_hint=INTENT_SCHEMA_HINT)
        else:
            raise LLMError("Model returned neither tool call nor content")
    else:
        parsed = await client.chat_json(messages, schema_hint=INTENT_SCHEMA_HINT)

    # Validate & coerce.
    parsed.setdefault("intent", "unknown")
    parsed["confidence"] = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    parsed.setdefault("speech", "")
    parsed.setdefault("quantity", 1)
    parsed.setdefault("candidates", [])
    parsed.setdefault("recommendations", [])
    return {"parsed": parsed, "summary": summary}


def _candidate_objects(db: Session, ids: list[int], fallback_query: str) -> list[models.Item]:
    if ids:
        items = db.query(models.Item).filter(models.Item.id.in_(ids)).all()
        order = {i: idx for idx, i in enumerate(ids)}
        items.sort(key=lambda x: order.get(x.id, 999))
        return items
    return search_items(db, fallback_query, limit=5)


def execute_intent(
    db: Session, text: str, parsed: dict[str, Any], cfg: AppConfig
) -> dict[str, Any]:
    """Materialize the parsed intent. Returns the IntentResult-shaped dict."""
    intent = parsed.get("intent", "unknown")
    confidence = float(parsed.get("confidence", 0.0))
    speech = parsed.get("speech", "")
    threshold = cfg.voice.confidence_threshold

    recs = parsed.get("recommendations") or []
    rec_purpose_by_id: dict[int, str] = {}
    for r in recs:
        try:
            rid = int(r.get("item_id"))
            rec_purpose_by_id[rid] = str(r.get("purpose") or "")
        except (TypeError, ValueError):
            continue

    base = {
        "intent": intent,
        "confidence": confidence,
        "speech": speech,
        "needs_confirmation": False,
        "pending_action": None,
        "candidates": [],
        "recommendations": [],
        "executed": False,
        "transaction_id": None,
        "raw": parsed,
    }

    # Build candidate display.
    cand_ids = parsed.get("candidates") or []
    if parsed.get("item_id") and parsed["item_id"] not in cand_ids:
        cand_ids = [parsed["item_id"], *cand_ids]
    # For assist intent, treat recommendations as the canonical candidate list.
    if intent == "assist":
        cand_ids = list(rec_purpose_by_id.keys()) or cand_ids
    candidates_objs = _candidate_objects(db, cand_ids, text)
    base["candidates"] = [
        {
            "item_id": it.id,
            "item_name": it.name,
            "location_path": location_path(it.location) if it.location else None,
            "score": 1.0 - (idx * 0.1),
        }
        for idx, it in enumerate(candidates_objs)
    ]
    if rec_purpose_by_id:
        # Order recommendations to match the candidate sort, then trail any extras.
        ordered_ids = [it.id for it in candidates_objs if it.id in rec_purpose_by_id]
        for rid in rec_purpose_by_id:
            if rid not in ordered_ids:
                ordered_ids.append(rid)
        base["recommendations"] = [
            {"item_id": rid, "purpose": rec_purpose_by_id.get(rid, "")}
            for rid in ordered_ids
        ]

    if intent == "assist":
        if not base["speech"]:
            names = [c["item_name"] for c in base["candidates"][:5]]
            base["speech"] = f"家里可能用得上的有: {', '.join(names) or '暂时没找到合适的'}"
        return base

    # Low confidence -> ask the user to confirm rather than mutating data.
    if intent in {"take_out", "put_in", "consume"} and confidence < threshold:
        base["needs_confirmation"] = True
        base["pending_action"] = {
            "intent": intent,
            "item_id": parsed.get("item_id"),
            "location_id": parsed.get("location_id"),
            "quantity": int(parsed.get("quantity") or 1),
        }
        if not speech:
            top = candidates_objs[0].name if candidates_objs else "这个物品"
            verb = {"take_out": "取出", "put_in": "存放", "consume": "消耗"}[intent]
            base["speech"] = f"我不太确定,你是想{verb}{top}吗"
        return base

    # Execute.
    if intent == "find":
        if candidates_objs:
            top = candidates_objs[0]
            # Group items that share the same display name with the top match — the user
            # likely wants to know all of them ("X 在 N 个地方").
            same_name = [c for c in candidates_objs if c.name == top.name]
            if len(same_name) >= 2:
                place_list = []
                for c in same_name:
                    p = location_path(c.location) or "未登记位置"
                    place_list.append(f"{p} (×{c.quantity})")
                base["speech"] = (
                    f"{top.name}在 {len(same_name)} 个地方都有: " + " ; ".join(place_list)
                )
                # Make sure the result's `candidates` includes ALL these same-name matches
                # so the frontend can highlight every one in 3D.
                base["candidates"] = [
                    {
                        "item_id": c.id,
                        "item_name": c.name,
                        "location_path": location_path(c.location) if c.location else None,
                        "score": 1.0 - (i * 0.05),
                    }
                    for i, c in enumerate(same_name)
                ]
            else:
                loc = location_path(top.location) or "未登记位置"
                if not speech:
                    if confidence >= 0.85:
                        base["speech"] = f"找到啦,{top.name}在{loc},库存{top.quantity}个"
                    else:
                        base["speech"] = f"你可能想找的是{top.name},放在{loc},库存{top.quantity}个"
        else:
            base["speech"] = speech or "暂未找到这种东西,可能还没登记进来"
        return base

    if intent == "list":
        return base

    if intent == "create_item":
        name = (parsed.get("item_name") or "").strip()
        if not name:
            base["speech"] = speech or "请告诉我物品名称"
            base["intent"] = "unknown"
            return base
        qty = int(parsed.get("quantity") or 1)
        loc_id = parsed.get("location_id")

        # Exact name or alias match → merge into existing item instead of
        # duplicating. Fuzzy / semantic matches are intentionally ignored here
        # so the user always gets a fresh record unless the name is identical.
        def _alias_set(aliases_str: str) -> set[str]:
            return {a.strip().lower()
                    for a in re.split(r"[,/，、;；]", aliases_str or "")
                    if a.strip()}

        name_lower = name.lower()
        existing: models.Item | None = None
        for candidate in db.query(models.Item).all():
            if (candidate.name or "").lower() == name_lower:
                existing = candidate
                break
            if name_lower in _alias_set(candidate.aliases or ""):
                existing = candidate
                break

        if existing:
            # Merge: add quantity, optionally update location.
            existing.quantity = (existing.quantity or 0) + qty
            existing.updated_at = datetime.now()
            if loc_id:
                existing.location_id = loc_id
            tx = models.Transaction(
                item_id=existing.id,
                action="put_in",
                quantity=qty,
                location_id=existing.location_id,
                note="语音创建(已合并)",
            )
            db.add(tx)
            db.commit()
            db.refresh(tx)
            base["executed"] = True
            base["transaction_id"] = tx.id
            loc_text = location_path(existing.location) if existing.location else "未指定位置"
            base["speech"] = speech or f"已找到同名物品,已将数量+{qty},现在{existing.name}共{existing.quantity}个,位置:{loc_text}"
        else:
            item = models.Item(
                name=name,
                location_id=loc_id,
                quantity=qty,
            )
            db.add(item)
            db.flush()
            tx = models.Transaction(
                item_id=item.id,
                action="put_in",
                quantity=item.quantity,
                location_id=item.location_id,
                note="语音创建",
            )
            db.add(tx)
            db.commit()
            db.refresh(tx)
            base["executed"] = True
            base["transaction_id"] = tx.id
            loc_text = location_path(item.location) if item.location else "未指定位置"
            base["speech"] = speech or f"记下啦,{name}放在{loc_text}了"
        return base

    if intent in {"take_out", "put_in", "consume"}:
        item_id = parsed.get("item_id")
        if not item_id and candidates_objs:
            item_id = candidates_objs[0].id
        if not item_id:
            base["intent"] = "unknown"
            base["speech"] = speech or "没找到这个物品,要不要先创建一个"
            return base
        item: models.Item | None = db.query(models.Item).get(item_id)
        if not item:
            base["intent"] = "unknown"
            base["speech"] = "物品不存在了"
            return base
        qty = int(parsed.get("quantity") or 1)
        loc_id = parsed.get("location_id") or item.location_id
        if intent == "take_out" or intent == "consume":
            item.quantity = max(0, (item.quantity or 0) - qty)
        else:  # put_in
            item.quantity = (item.quantity or 0) + qty
            if parsed.get("location_id"):
                item.location_id = parsed["location_id"]
        item.updated_at = datetime.now()
        tx = models.Transaction(
            item_id=item.id,
            action=intent,
            quantity=qty,
            location_id=loc_id,
            note="语音操作",
        )
        db.add(tx)
        db.commit()
        db.refresh(tx)
        base["executed"] = True
        base["transaction_id"] = tx.id
        if not base["speech"]:
            if intent == "take_out":
                base["speech"] = f"已取出{item.name} {qty}个,用完记得归位哦,当前余量{item.quantity}"
            elif intent == "consume":
                base["speech"] = f"已记录用完{item.name} {qty}个,剩{item.quantity}个"
            else:
                loc_text = location_path(item.location) if item.location else "原位置"
                base["speech"] = f"好的,已存入{item.name} {qty}个到{loc_text},现在共{item.quantity}件"
        return base

    # unknown
    if not base["speech"]:
        base["speech"] = "没听懂呢,能换个说法吗"
    return base
