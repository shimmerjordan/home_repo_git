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
    # 只读的 find 已经算出答案了 (op["speech"], 见 intent.py::_do_find), 它不需要
    # 确认 —— 先把答案给出来。否则"苹果在哪, 把螺丝刀删了"这种一句话里的查询
    # 结果会被整个丢掉: 渲染成一行没用的"查找 苹果 ×1", 确认后 _decisions_from_plan
    # 又把 find 跳过, 答案就再也没了。
    answers = [o["speech"] for o in ops
               if o.get("intent") == "find" and o.get("speech")]
    todo = [o for o in ops if o.get("intent") != "find"]
    lines: list[str] = []
    if answers:
        lines.extend(answers)
        lines.append("")
    lines.append(f"这次操作需要确认 ({reason}):")
    for i, op in enumerate(todo, 1):
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


def format_result(result: dict[str, Any]) -> str:
    """把一次执行结果渲染成群消息纯文本。

    从 telegram.py::_format_reply 移植过来的 —— 以前三端各有一份渲染
    (telegram 一份、feishu 一个转调它的壳、dingtalk 另写一份 markdown 表格),
    现在收成这一份。去掉了 Markdown 标记: 飞书的 text 消息不解析它, 星号会
    原样显示; 而带 parse_mode 的 Telegram 遇到物品名里的下划线会直接 400
    (用户一条回复都收不到)。
    """
    lines: list[str] = []
    if result.get("speech"):
        lines.append(result["speech"])
    cands = result.get("candidates") or []
    recs = result.get("recommendations") or []
    cm = {c["item_id"]: c for c in cands}
    if recs:
        lines.append("")
        lines.append("推荐用品:")
        for r in recs:
            c = cm.get(r["item_id"]) or {}
            name = c.get("item_name", f"#{r['item_id']}")
            purpose = r.get("purpose") or ""
            loc = c.get("location_path") or "未指定位置"
            lines.append(f"· {name} — {purpose} ({loc})")
    elif cands:
        lines.append("")
        lines.append("位置:")
        for c in cands[:10]:
            lines.append(f"· {c['item_name']} — {c.get('location_path') or '未指定位置'}")
    if result.get("executed"):
        lines.append("")
        lines.append("✅ 已记录")
    return "\n".join(lines).strip() or "（无内容）"
