"""三个群机器人 (钉钉 / Telegram / 飞书) 共用的一次消息处理。

各端 handler 只负责: 收消息、取出 (chat_id, sender_id, text)、把返回的文本发回去。
解析、风险判定、待确认、执行, 全在这里 —— 以前这段逻辑在三个文件里各抄了一份
(包括那句把 confidence 抬到 1.0 的)。
"""
from __future__ import annotations

from typing import Any

from ..config import AppConfig
from ..llm import intent as I
from ..llm.client import LLMError
from . import botfmt, pending, risk
from .logbuffer import app_log


def _decisions_from_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """把方案里每条的 selected 变成 apply 要的 decision。"""
    out = []
    for op in plan.get("operations") or []:
        if op.get("intent") == "find":
            continue
        out.append({
            "intent": op.get("intent"),
            "option_key": op.get("selected") or "skip",
            "new_item_name": op.get("new_name_default") or op.get("item_name"),
            "location_id": op.get("location_id"),
            "location_name": op.get("location_name"),
            "quantity": op.get("quantity") or 1,
        })
    return out


async def handle_bot_message(
    channel: str, chat_id: str, sender_id: str, text: str,
    db, cfg: AppConfig,
) -> str:
    """返回要发回群里的文本。"""
    reply_kind = pending.classify_reply(text)
    if reply_kind in ("yes", "no"):
        plan = pending.take(channel, chat_id, sender_id)
        if plan is None:
            return "没有待确认的操作 (可能已经超过 5 分钟失效了)。"
        if reply_kind == "no":
            return "已取消, 什么都没改。"
        base = dict(plan)
        result = I.apply_operations(db, plan.get("_text") or "",
                                    _decisions_from_plan(plan), base)
        db.commit()
        return botfmt.format_result(result)

    # 说了别的 —— 旧方案作废, 当新指令处理。
    pending.drop(channel, chat_id, sender_id)
    try:
        out = await I.parse_intent(text, db, cfg)
    except LLMError as exc:
        app_log.warning("%s: 解析失败 %s", channel, exc)
        return f"解析失败: {exc}"
    parsed = out["parsed"]

    plan = I.execute_intent(db, text, parsed, cfg, plan_only=True)

    # 位置歧义先拦下来 —— 它虽然也是高风险, 但"确认"解不了 (见 botfmt 里的注释),
    # 存成待确认只会让用户确认完看到"没执行"。直接问清是哪个位置。
    ambig = botfmt.format_location_ambiguity(plan)
    if ambig:
        return ambig

    high, why = risk.plan_risk(plan.get("operations") or [])
    if high:
        plan["_text"] = text
        pending.put(channel, chat_id, sender_id, plan)
        return botfmt.format_plan(plan, why)

    result = I.execute_intent(db, text, parsed, cfg)
    if result.get("needs_confirmation"):
        # execute_intent 自己的置信度门槛把它拦下了 —— 没写库, 只回了一句
        # "我不太确定,你是想…吗"。必须把方案存起来: 否则用户回"确认"时
        # pending 是空的, 只会得到"没有待确认的操作", 问了等于没问,
        # 而且这条操作再也没法触发。
        # (plan_risk 只看匹配质量和操作种类, 不看 LLM 自报的 confidence,
        #  所以这道门槛是它覆盖不到的另一条路。)
        plan["_text"] = text
        pending.put(channel, chat_id, sender_id, plan)
        return botfmt.format_plan(plan, "AI 对这句话不太确定")
    db.commit()
    return botfmt.format_result(result)
