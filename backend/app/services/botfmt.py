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
        if op.get("selected") == "skip":
            # selected=="skip" 意味着这条什么都不会发生 (没识别出物品名,
            # 或库里没有又不能新建)。渲染得和可执行条目一样, 用户会以为
            # 确认下去它也会被执行。
            seg += " — 这条会跳过, 不会真的执行"
        elif op.get("matched_by") in ("fuzzy", "ambiguous"):
            # fuzzy/ambiguous 正是 plan_risk 判高风险的两类: 系统替用户从
            # 多个候选里挑了一条。只写用户说的原词 (item_name) 没法让人
            # 判断到底会改哪条记录 —— 必须把 candidates 里真正命中的那条
            # 亮出来 (按 item_id 匹配, 而不是重新按名字猜)。
            cand = next((c for c in (op.get("candidates") or [])
                         if c.get("item_id") == op.get("item_id")), None)
            if cand:
                cand_loc = cand.get("location_path") or "未指定位置"
                seg += f" → 实际会改: {cand.get('item_name')} ({cand_loc})"
            if op.get("reason"):
                seg += f" ({op['reason']})"
        lines.append(seg)
    lines.append("回复 确认 执行, 回复 取消 放弃。5 分钟内有效。")
    return "\n".join(lines)


def format_location_ambiguity(plan: dict[str, Any]) -> str | None:
    """位置没说准时的回复; 没有这类操作就返回 None。

    为什么整句都不执行: 这一步只是方案 (plan_only), 一个字都还没落库。只挑没歧义
    的执行会让用户面对"一半做了一半没做"的状态, 而他手上只有"再说一遍"这一个动作。
    所以整句退回, 但必须把**每一条**都列出来 —— 以前只列有歧义的那几条, 用户
    以为其余的做成了, 那条正常操作就此永久丢失。
    """
    ops = plan.get("operations") or []
    if not any(o.get("location_options") for o in ops):
        return None
    lines = ["这句话我都没执行, 因为位置没说准:"]
    for i, op in enumerate(ops, 1):
        name = op.get("item_name") or "?"
        opts = op.get("location_options") or []
        if opts:
            asked = op.get("location_name") or "?"
            lines.append(f"{i}. {name} → 「{asked}」有这几个:")
            # 每条候选独占一行、带序号 —— 候选的完整路径本身就用 " / " 分隔层级,
            # 如果再用 " / " 把多个候选拼成一行, 读起来像一条很深的路径,
            # 而不是几个平级候选 (这条恢复路径唯一的出路就是让用户照着candidates重说)。
            for j, o in enumerate(opts[:5], 1):
                path = o.get("path") or o.get("name", "?")
                lines.append(f"     ({j}) {path}")
        else:
            lines.append(f"{i}. {name} — 这条本来没问题")
    lines.append("把位置说清楚, 整句再说一遍。")
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
    cm = {c.get("item_id"): c for c in cands}
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
            lines.append(f"· {c.get('item_name') or '?'} — "
                         f"{c.get('location_path') or '未指定位置'}")
    if result.get("executed"):
        lines.append("")
        lines.append("✅ 已记录")
    return "\n".join(lines).strip() or "（无内容）"
