"""方案风险判定 —— 决定群机器人是秒执行还是先问一句。

低风险 (秒回): 精确命中的存取、明确的新建。日常绝大多数。
高风险 (先出方案): 删档、模糊命中已有物品、一次改三条以上、位置没定。
这四类正是误操作真正会造成损失的地方。
"""
from __future__ import annotations

from typing import Any

_MUTATIONS = {"take_out", "put_in", "consume", "create_item", "delete_item"}
MULTI_THRESHOLD = 3

# 这两种"没认准"是同一类风险: 系统从多个候选里挑了一个替用户做主。
# fuzzy = 只有名字相近的; ambiguous = 多处同名, _plan_one 预选了第一条。
_UNSURE_MATCHES = {"fuzzy", "ambiguous"}


def plan_risk(operations: list[dict[str, Any]]) -> tuple[bool, str]:
    """返回 (是否高风险, 中文原因)。低风险时原因是空串。"""
    ops = operations or []
    mutations = [o for o in ops if o.get("intent") in _MUTATIONS]

    if any(o.get("intent") == "delete_item" for o in mutations):
        return True, "包含删除物品档案"
    if any(o.get("matched_by") in _UNSURE_MATCHES for o in mutations):
        return True, "有物品没认准, 是从几个候选里挑的"
    if any(o.get("location_options") for o in mutations):
        return True, "有操作的目标位置不明确"
    if len(mutations) >= MULTI_THRESHOLD:
        return True, f"一次要改 {len(mutations)} 条记录"
    return False, ""
