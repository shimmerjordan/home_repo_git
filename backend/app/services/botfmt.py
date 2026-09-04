"""把待确认方案渲染成群消息文本。

必须是**纯文本**: 飞书走 msg_type="text", Markdown 星号会原样显示出来;
钉钉虽然支持 markdown, 但三端共用一份渲染比各写一份更不容易走样。
所以只用序号、空格和箭头, 不用任何标记语法。
"""
from __future__ import annotations

from typing import Any

_VERB = {
    "take_out": "取出", "put_in": "存入", "consume": "用掉",
    "create_item": "新建", "delete_item": "删除档案", "find": "查找",
}


def format_plan(plan: dict[str, Any], reason: str) -> str:
    ops = plan.get("operations") or []
    lines = [f"这次操作需要确认 ({reason}):"]
    for i, op in enumerate(ops, 1):
        verb = _VERB.get(op.get("intent"), op.get("intent") or "操作")
        name = op.get("item_name") or "?"
        qty = op.get("quantity") or 1
        seg = f"{i}. {verb} {name} ×{qty}"
        loc = op.get("location_path") or op.get("location_name")
        if op.get("location_options"):
            names = "/".join(o.get("name", "?") for o in op["location_options"][:3])
            seg += f" → 位置不明确 ({names})"
        elif loc:
            seg += f" → {loc}"
        if op.get("matched_by") in ("fuzzy", "ambiguous"):
            seg += " (没认准, 从几个候选里挑的)"
        lines.append(seg)
    lines.append("回复 确认 执行, 回复 取消 放弃。5 分钟内有效。")
    return "\n".join(lines)


def format_location_ambiguity(plan: dict[str, Any]) -> str | None:
    """位置没说准时的回复; 没有这类操作就返回 None。

    为什么单独一条路而不走"确认": 确认是二选一, 解不了三选一的位置。
    方案里位置歧义的那条 location_id 本来就是 None, 用户回"确认"之后
    apply 会拿同一个名字重新解析、得到同样的歧义, 回一句"没执行" ——
    用户确认了却什么也没发生, 还不知道该干什么。所以直接问清楚。
    """
    ops = plan.get("operations") or []
    bad = [o for o in ops if o.get("location_options")]
    if not bad:
        return None
    lines = ["位置没说准, 这几条没执行:"]
    for i, op in enumerate(bad, 1):
        name = op.get("item_name") or "?"
        asked = op.get("location_name") or "?"
        opts = " / ".join(o.get("path") or o.get("name", "?")
                          for o in op["location_options"][:5])
        lines.append(f"{i}. {name} → 「{asked}」可能是: {opts}")
    lines.append("说清楚是哪一个, 再说一次。")
    return "\n".join(lines)
