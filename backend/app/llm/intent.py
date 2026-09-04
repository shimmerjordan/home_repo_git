"""Voice-text → structured intent → DB action.

Pipeline:
  1. Build a compact inventory summary scoped to the query.
  2. Ask the LLM (via OpenAI-compatible chat) to choose an intent + parameters.
     Prefer tool-calling; fall back to JSON mode for providers that don't support tools.
  3. Validate the response, compute confidence, optionally execute.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from .. import models
from ..config import AppConfig
from ..services.inventory import apply_quantity_delta, location_path, search_items
from ..services.logbuffer import app_log
from ..services.summary import build_summary

log = logging.getLogger("storage.intent")
from .client import LLMClient, LLMError, LLMTruncated


SYSTEM_PROMPT = """你是家庭仓储管家的语义解析器, 同时要给出温暖、口语化的中文回答。
你的工作:
1. 阅读用户语句和当前的库存摘要
2. 注意位置层级最上层可能是"家"(如 我家 / 老家 / 父母家),"家"下面才是房间。用户没特别说"在老家"之类的话, 默认指主要居住的家
3. 选择一个意图: find / take_out / put_in / consume / list / create_item / delete_item / assist / unknown
   - take_out: "借出" — 我拿出来用一下, **稍后需要归位**(如 "我拿了卷尺"/"借走螺丝刀"/"拿出充电宝")
   - consume:  "消耗完" — 用完了/扔了/吃了/送人了, **不会归位**(如 "我喝完了一瓶水"/"用完最后一支牙膏"/"吃了两片药"/"扔了过期面包")
   - delete_item: "把这条记录删掉" — 用户要**移除物品档案本身**, 不是消耗库存
     (如 "把充电宝从库里删了"/"删除这条记录"/"这个物品不要了, 删掉")
     与 consume 的区别: consume 是东西用完了但档案还在(可以再补货); delete_item 是这条记录压根不该存在
     后端不会立即执行, 会让用户在界面上二次确认
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

批量操作 (最重要, 这里出错代价最大):
- 用户一句话涉及**多个物品**时 (无论查询还是操作), 必须把每个物品**各自一条**列进 operations 数组,
  一条不落。绝不能只处理第一个, 也绝不能把多个物品塞进同一条的 item_name 里
- 先在心里数一遍用户提到了几个物品, operations 的条数必须等于这个数
- operations 里每条包含: intent / item_id / item_name / location_id / location_name / quantity / force_new
- **数量各自算**: "两瓶水和三包纸巾" -> 水 quantity=2, 纸巾 quantity=3。没说数量就是 1
- 例1 "把手表、铅笔、橡皮放进书桌1" -> 三条 put_in; 位置从"位置列表"里找 id 填 location_id, 找不到就填 location_name="书桌1"
- 例2 "我消耗了一瓶水和两片药" -> 两条 consume (quantity 分别 1 和 2)
- 例3 "手表和铅笔在哪" -> 两条 find
- 例4 "拿了卷尺, 顺便把螺丝刀放回工具箱" -> 一条 take_out + 一条 put_in
- 例5 "我用完了洗手液, 拿了螺丝刀和卷尺, 把两个充电器放回书桌1" -> 四条 (consume/take_out/take_out/put_in)
- 顶层 intent 填第一个操作的意图, speech 一句话总结全部操作
- 只有一个物品/操作时 operations 留空, 继续用顶层字段

物品匹配 (第二重要):
- **不确定就不要猜 item_id**。把 item_id 留空 (null)、把 item_name 填成用户说的原词、
  把你觉得可能的几个 id 放进 candidates, 并调低 confidence。后端会让用户在界面上挑,
  你猜错了会把库存加到别人头上, 而留空只是多点一下
- 只有当库存里那条记录的**名称或别名和用户说的基本一致**时才填 item_id
- 语义相近但不是同一样东西 (充电宝 vs 充电器 / 洗发水 vs 洗手液 / 螺丝刀 vs 螺丝) 一律**不要**填 item_id

force_new (全新物品, 不要匹配):
- 用户说"**新增/新建/添加/录入/新买的/记一个新的** X" 时, 说明 X 是全新物品:
  intent 用 create_item, 并且 **force_new=true**, item_id 必须留空
- 这种情况下就算库存里有同名的也不要匹配 —— 用户的意思是再建一条新档案
- 反之 "把X放进Y" / "X放回Y" 是归位/入库, 不是新增: 用 put_in, force_new=false
- "又买了两瓶水" / "补货" 这类是补库存: 用 put_in, 优先匹配已有档案 (包括"库存为0的旧档案"那一段)
- **多物品句里, force_new 要每条操作各自判断, 不能因为句子里出现一次"新增"就全部设 true**:
  "新增手表和铅笔到书桌1" -> 两条都是 force_new=true (两个都是新东西);
  "新增一个订书机到书桌1, 再把卷尺也放进去" -> 只有订书机 force_new=true, 卷尺是已有物品收纳, 用 put_in / force_new=false

否定 (用户明确说不要的物品):
- 用户说"别拿/不要/不用/除了/甭" 某个物品时, 那个物品**不要出现在 operations 里**,
  也不要出现在顶层字段: "拿卷尺, 别拿螺丝刀" 只输出卷尺这一条, 螺丝刀完全不提

数量与量词:
- 一打=12; 一双/一对/一副 记的是这个计数单位本身、不换算成只数 (比如"两双袜子" quantity=2);
  "半瓶/半包"这类按 1 记 (quantity 只能是整数, 不做分数)

注意:
- "我刚拿了X"对应 take_out (借出, 待归位); "我用完了X" / "X 喝完了" / "扔了X" 对应 consume (永久减库存, 不待归位)
- "我把X放在Y了"对应 put_in (归位; 如果有 take_out 待归位的同名物品, 自动抵消)
- create_item 必须包含 item_name 和(可选) location_id
- 如果完全无法理解, intent=unknown, confidence=0
"""


INTENT_SCHEMA_HINT = """{
  "intent": "find|take_out|put_in|consume|list|create_item|delete_item|assist|unknown",
  "confidence": 0.0,
  "speech": "string (中文, 给用户的简短回答)",
  "item_id": null,                // 已存在物品 id (find/take_out/put_in)
  "item_name": null,              // create_item 用
  "location_id": null,            // put_in / create_item 可用
  "quantity": 1,
  "candidates": [123, 456],       // 备选 item_id, 当不确定时给出
  "operations": [                 // 一句话涉及多个物品时列出全部 (含 find 查询), 单操作留空
    {"intent": "consume|find|take_out|put_in|create_item|delete_item", "item_id": 12, "item_name": "矿泉水",
     "location_id": null, "location_name": null, "quantity": 1, "force_new": false}
  ],
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
                        "enum": ["find", "take_out", "put_in", "consume", "list", "create_item", "delete_item", "assist", "unknown"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "speech": {"type": "string"},
                    "item_id": {"type": ["integer", "null"]},
                    "item_name": {"type": ["string", "null"]},
                    "location_id": {"type": ["integer", "null"]},
                    "location_name": {
                        "type": ["string", "null"],
                        "description": "目标位置名称 (位置列表里找不到 id 时填)",
                    },
                    "quantity": {"type": "integer", "default": 1},
                    "force_new": {
                        "type": "boolean",
                        "default": False,
                        "description": "true = 用户说的是全新物品(新增/新建/添加/录入/新买的), 不要匹配任何已有物品",
                    },
                    "candidates": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "default": [],
                    },
                    "operations": {
                        "type": "array",
                        "description": "一句话涉及多个物品时列出全部操作 (查询/取出/存入/消耗/新建都算); 单操作留空用顶层字段",
                        "items": {
                            "type": "object",
                            "properties": {
                                "intent": {
                                    "type": "string",
                                    "enum": ["find", "take_out", "put_in", "consume", "create_item", "delete_item"],
                                },
                                "item_id": {"type": ["integer", "null"]},
                                "item_name": {"type": ["string", "null"]},
                                "location_id": {"type": ["integer", "null"]},
                                "location_name": {
                                    "type": ["string", "null"],
                                    "description": "目标位置名称 (位置列表里找不到 id 时填)",
                                },
                                "quantity": {"type": "integer", "default": 1},
                                "force_new": {
                                    "type": "boolean",
                                    "default": False,
                                    "description": "true = 全新物品, 不要匹配已有物品。每条操作独立判断, "
                                                   "不要因为句子里出现一次'新增'就给所有条目都设 true",
                                },
                            },
                            "required": ["intent"],
                        },
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


# delete_item 也算 mutation(低置信度要确认), 但它在执行层是唯一"只解析不执行"的动作。
MUTATION_INTENTS = {"take_out", "put_in", "consume", "create_item", "delete_item"}
# Batch entries may additionally be read-only lookups ("手表和铅笔在哪").
BATCH_INTENTS = MUTATION_INTENTS | {"find"}


# "新增/新建/添加..." 这类措辞 = 全新物品, 不要往已有档案上匹配 (用户明确要求)。
# 只信 prompt 是不够的 —— 模型偶尔会把"新增X到Y"解析成 put_in, 所以后端再兜一道。
FORCE_NEW_VERBS = re.compile(r"新增|新建|新添|添加|录入|新登记|记一个新|新买的|买了个新")
# 反例: 这些措辞是补库存/归位, 即使句中出现"买"也**不能**当成全新物品。
RESTOCK_HINTS = re.compile(r"补货|补充|又买|再买|补上|添满")

# 分句边界: 标点, 以及"再/然后/接着/另外"这类起新动作的连接词。
# 整句 force_new 兜底只作用于含"新增"措辞的那个分句 —— "新增跑步机到书房,
# 再放两个螺丝到工具箱" 里"新增"只管跑步机, 螺丝是另一件事。把它也升级成
# 新建会给已有的螺丝静默建一条重复档案, 而不升级的话最坏只是回一句
# "库里没有X, 要新建吗" (T4 之后 put_in 找不到就是这个行为) —— 问比乱写安全。
_CLAUSE_SPLIT = re.compile(r"[,，;；。!!?？\n]|再|然后|接着|另外|顺便")


def _force_new_clause(utterance: str) -> str:
    """含"新增"措辞的那个分句; 找不到就返回空串 (那就一条都不升级, 偏保守)。"""
    for part in _CLAUSE_SPLIT.split(utterance or ""):
        if FORCE_NEW_VERBS.search(part):
            return part
    return ""

# 量词 → 倍数。只收有确定倍数的; "些/若干/几"这类模糊词不进表 (维持 1)。
#
# 为什么"双/对/副"是 1 而不是 2: 它们的计数单位就是"双"本身 —— 家里记袜子记的是
# 几双, 不是几只, 所以"两双袜子"记 2。放进表里不是为了乘, 是为了**认出**这个数字:
# 不认识"双"的话, "拿两双袜子" 会走到默认值 1, 那个 2 就丢了。
# "打"不同: 没人会记"1 打铅笔", 一打就该展开成 12。
_QUANTIFIER_MAP = {"打": 12, "双": 1, "对": 1, "副": 1}
_QUANTIFIER_RE = re.compile(
    r"([一二两三四五六七八九十\d]+)\s*(" + "|".join(_QUANTIFIER_MAP) + r")")
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 否定: 只处理"整句里明确不要某个东西"这种情形。条件句 ("如果没有就…")
# 和指代 ("把它放回去") 不在范围内 —— 那需要多轮上下文, 是另一件事。
NEGATION_HINTS = re.compile(r"别|不要|不用|除了|甭")


def _looks_force_new(text: str) -> bool:
    """整句里是否有"这是个新物品"的明确措辞。"""
    if not text:
        return False
    if RESTOCK_HINTS.search(text):
        return False
    return bool(FORCE_NEW_VERBS.search(text))


def _cn_int(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    if len(s) == 1:
        return _CN_NUM.get(s)
    return None


def _quantity_from_text(utterance: str, item_name: str, given: int | None) -> int:
    """LLM 没给数量时, 从量词里兜一个。给了就用它的。

    量词要挑**离这个物品名最近的那个前置量词**: "拿一打铅笔和两双袜子" 里
    铅笔是 12、袜子是 2。只取整句第一个匹配的话, 袜子会跟着变成 12。
    """
    if given:
        return given
    text = utterance or ""
    matches: list[tuple[int, int]] = []
    for m in _QUANTIFIER_RE.finditer(text):
        n = _cn_int(m.group(1))
        if n:
            matches.append((m.end(), n * _QUANTIFIER_MAP[m.group(2)]))
    if not matches:
        return 1
    idx = text.find(item_name) if item_name else -1
    if idx >= 0:
        before = [q for end, q in matches if end <= idx]
        if before:
            return before[-1]
    return matches[0][1]


def _is_negated(utterance: str, item_name: str) -> bool:
    """物品名前面 6 个字以内出现否定词 —— "拿卷尺, 别拿螺丝刀" 里只有螺丝刀被否定。"""
    if not item_name or not NEGATION_HINTS.search(utterance or ""):
        return False
    idx = utterance.find(item_name)
    if idx < 0:
        return False
    return bool(NEGATION_HINTS.search(utterance[max(0, idx - 6):idx]))


def _coerce_int(val: Any) -> int | None:
    """LLM 偶尔把 id 给成字符串 "12" 或 "null"。"""
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _op_from_parsed(parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a single operation dict from a top-level parsed intent (or None)."""
    if parsed.get("intent") not in BATCH_INTENTS:
        return None
    return {
        "intent": parsed["intent"],
        "item_id": _coerce_int(parsed.get("item_id")),
        "item_name": parsed.get("item_name"),
        "location_id": _coerce_int(parsed.get("location_id")),
        "location_name": parsed.get("location_name"),
        "quantity": max(1, _coerce_int(parsed.get("quantity")) or 1),
        "force_new": bool(parsed.get("force_new")),
    }


# 少数模型会把 intent 写成同义词, 映射回来比直接丢掉好。
_INTENT_ALIASES = {
    "take": "take_out", "takeout": "take_out", "borrow": "take_out", "remove": "take_out",
    "put": "put_in", "putin": "put_in", "store": "put_in", "add": "put_in", "restock": "put_in",
    "use": "consume", "used": "consume", "finish": "consume", "discard": "consume",
    "create": "create_item", "new": "create_item", "new_item": "create_item",
    "delete": "delete_item", "remove_item": "delete_item",
    "search": "find", "lookup": "find", "query": "find", "where": "find",
}


def _op_key(op: dict[str, Any]) -> tuple:
    """去重键。多 tool_call 合并 (见 parse_intent) 会产生完全重复的条目。"""
    return (
        op["intent"], op.get("item_id"),
        (op.get("item_name") or "").strip().lower(),
        op.get("location_id"), (op.get("location_name") or "").strip().lower(),
        op["quantity"],
    )


def _normalize_operations(parsed: dict[str, Any], utterance: str = "") -> list[dict[str, Any]]:
    """Coerce parsed['operations'] into a clean list of batch ops.

    以前这里对任何看不懂的条目直接 `continue` —— 静默丢一条操作, 用户完全无从察觉。
    现在: 别名映射一次, 丢弃的写进 parsed['_dropped_ops'] 让上层能记日志, 并去重。
    """
    ops: list[dict[str, Any]] = []
    dropped: list[Any] = []
    seen: set[tuple] = set()
    sentence_force_new = _looks_force_new(utterance)
    for raw in parsed.get("operations") or []:
        if not isinstance(raw, dict):
            dropped.append(raw)
            continue
        intent = str(raw.get("intent") or "").strip()
        intent = _INTENT_ALIASES.get(intent.lower(), intent)
        if intent not in BATCH_INTENTS:
            dropped.append(raw)
            continue
        op = {
            "intent": intent,
            "item_id": _coerce_int(raw.get("item_id")),
            "item_name": raw.get("item_name"),
            "location_id": _coerce_int(raw.get("location_id")),
            "location_name": raw.get("location_name"),
            "quantity": _quantity_from_text(utterance, raw.get("item_name") or "",
                                            _coerce_int(raw.get("quantity"))),
            "force_new": bool(raw.get("force_new")),
        }
        # create_item 本身就是"建一条新的", 语义上等价于 force_new。
        if op["intent"] == "create_item":
            op["force_new"] = True
        # 反过来: 模型给了 force_new=true 却仍写成 put_in (字段没同步) ——
        # 按它自己给的 force_new 校正 intent, 只改这一条自己的, 不影响其它条目。
        elif op["intent"] == "put_in" and op["force_new"]:
            op["intent"] = "create_item"
        if _is_negated(utterance, op["item_name"] or ""):
            dropped.append(op)
            continue
        if _op_key(op) in seen:
            continue
        seen.add(_op_key(op))
        ops.append(op)
    # 句子明确是新增措辞, 而 LLM 一条都没标 force_new —— 说明它没做这个判断,
    # 那就按"新增"所在的那个分句来兜底。只要它标了任何一条, 就尊重它的判断,
    # 一个字都不改。
    if sentence_force_new and ops and not any(o.get("force_new") for o in ops):
        clause = _force_new_clause(utterance)
        for o in ops:
            name = o.get("item_name") or ""
            if o["intent"] == "put_in" and name and name in clause:
                o["intent"] = "create_item"
                o["force_new"] = True
    if dropped:
        parsed["_dropped_ops"] = dropped
    return ops


# ---- per-session lookup cache -------------------------------------------------
# plan / apply 一批 5 个物品会把 Location.all() / Item.all() 各查 5~10 遍。这里按 session
# 缓存整表, 任何 flush / rollback 立刻清空 —— 语义与"每次现查"完全一致 (session 是
# autoflush=False, 查询本来就只看得到已 flush 的数据)。
from sqlalchemy import event as _sa_event
from sqlalchemy.orm import Session as _SaSession

_CACHE_KEY = "_intent_cache"


def _cache(db: Session) -> dict:
    return db.info.setdefault(_CACHE_KEY, {})


def _clear_cache(session, *_args) -> None:
    session.info.pop(_CACHE_KEY, None)


_sa_event.listen(_SaSession, "after_flush", _clear_cache)
_sa_event.listen(_SaSession, "after_rollback", _clear_cache)
_sa_event.listen(_SaSession, "after_commit", _clear_cache)


def _all_locations(db: Session) -> list[models.Location]:
    c = _cache(db)
    if "locations" not in c:
        c["locations"] = db.query(models.Location).all()
    return c["locations"]


def _all_items(db: Session) -> list[models.Item]:
    c = _cache(db)
    if "items" not in c:
        c["items"] = db.query(models.Item).all()
    return c["items"]


def _resolve_location(
    db: Session, ref: dict[str, Any]
) -> tuple[int | None, list[models.Location]]:
    """把 location_id / location_name 解析成真实 Location id。

    返回 (命中id, 歧义候选)。规则: 精确名 (大小写不敏感) 直接胜出; 没有精确名时
    收集**全部**子串候选 —— 恰好一个就用它, 多个就一个都不选, 把候选交回给调用方
    让用户挑。老实现取第一个子串命中, "书桌1"会静默落到"书桌10"上。
    """
    if ref.get("location_id"):
        loc = db.get(models.Location, ref["location_id"])
        if loc:
            return loc.id, []
    name = (ref.get("location_name") or "").strip()
    if not name:
        return None, []
    name_lower = name.lower()
    candidates: list[models.Location] = []
    for loc in _all_locations(db):
        ln = (loc.name or "").lower()
        if ln == name_lower:
            return loc.id, []
        if (name_lower in ln or ln in name_lower
                or name_lower in location_path(loc).lower()):
            candidates.append(loc)
    if len(candidates) == 1:
        return candidates[0].id, []
    if candidates:
        # 名字长度最接近的排前面, 同长按全路径排 —— 只影响展示顺序, 不影响"不猜"。
        candidates.sort(key=lambda l: (abs(len(l.name or "") - len(name)),
                                       location_path(l)))
        return None, candidates
    return None, []


# 截断重试的放大倍数与硬上限。一次翻 4 倍足够覆盖"思考块吃掉大半预算"的情况。
TRUNCATION_RETRY_FACTOR = 4
TRUNCATION_RETRY_CAP = 16384


async def parse_intent(text: str, db: Session, cfg: AppConfig) -> dict[str, Any]:
    """解析一次意图。遇到输出被 max_tokens 截断时用更大的预算重试一次。

    截断为什么必须重试而不能将就: tool_use 的 input JSON 断在一半时,
    operations 数组可能整个丢空 —— 用户说了四件事, 后端一件都没收到,
    却仍然按"成功"往下走。宁可多打一次请求。
    """
    try:
        return await _parse_once(text, db, cfg, max_tokens=None)
    except LLMTruncated as exc:
        bigger = min(TRUNCATION_RETRY_CAP,
                     max(exc.max_tokens, cfg.llm.max_tokens) * TRUNCATION_RETRY_FACTOR)
        log.warning("意图解析被截断 (max_tokens=%s), 用 %s 重试一次: %s",
                    exc.max_tokens, bigger, text[:60])
        app_log.warning("AI 输出被 max_tokens=%s 截断, 已用 %s 重试 —— "
                        "建议到设置页把 max_tokens 调到 4096 以上",
                        exc.max_tokens, bigger)
        return await _parse_once(text, db, cfg, max_tokens=bigger)


async def _parse_once(
    text: str, db: Session, cfg: AppConfig, *, max_tokens: int | None
) -> dict[str, Any]:
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
        result = await client.chat(messages, tools=TOOLS,
                                   force_tool="submit_intent", max_tokens=max_tokens)
        if result["tool_calls"]:
            calls = [tc["arguments"] for tc in result["tool_calls"] if tc.get("arguments")]
            parsed = calls[0]
            # Some models emit one submit_intent call per operation instead of
            # filling the operations array — merge every extra call in, so a
            # batch utterance ("我消耗了A和B") never silently drops operations.
            if len(calls) > 1:
                ops = list(parsed.get("operations") or [])
                if not ops:
                    first = _op_from_parsed(parsed)
                    if first:
                        ops.append(first)
                for extra in calls[1:]:
                    extra_ops = extra.get("operations") or []
                    if not extra_ops:
                        one = _op_from_parsed(extra)
                        extra_ops = [one] if one else []
                    ops.extend(extra_ops)
                if ops:
                    parsed["operations"] = ops
        elif result["content"]:
            # Fallback if model ignored the tool.
            parsed = await client.chat_json(messages, schema_hint=INTENT_SCHEMA_HINT,
                                            max_tokens=max_tokens)
        else:
            raise LLMError("Model returned neither tool call nor content")
    else:
        parsed = await client.chat_json(messages, schema_hint=INTENT_SCHEMA_HINT,
                                        max_tokens=max_tokens)

    # Validate & coerce.
    parsed.setdefault("intent", "unknown")
    try:
        parsed["confidence"] = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    except (TypeError, ValueError):
        parsed["confidence"] = 0.0
    parsed.setdefault("speech", "")
    parsed.setdefault("quantity", 1)
    parsed.setdefault("candidates", [])
    parsed.setdefault("recommendations", [])
    parsed.setdefault("operations", [])
    parsed.setdefault("force_new", False)
    return {"parsed": parsed, "summary": summary}


def _alias_set(aliases_str: str) -> set[str]:
    return {a.strip().lower()
            for a in re.split(r"[,/，、;；]", aliases_str or "")
            if a.strip()}


def _find_exact_item(db: Session, name: str) -> models.Item | None:
    """Exact name/alias match (case-insensitive). Fuzzy matches intentionally ignored."""
    name_lower = name.lower()
    for candidate in _all_items(db):
        if (candidate.name or "").lower() == name_lower:
            return candidate
        if name_lower in _alias_set(candidate.aliases or ""):
            return candidate
    return None


def _cand_dicts(items: list[models.Item]) -> list[dict[str, Any]]:
    """Serialize items as IntentCandidate dicts, best match first."""
    return [
        {
            "item_id": it.id,
            "item_name": it.name,
            "location_path": location_path(it.location) if it.location else None,
            "score": 1.0 - (i * 0.05),
        }
        for i, it in enumerate(items)
    ]


def _find_item_for_op(
    db: Session, op: dict[str, Any]
) -> tuple[models.Item | None, list[models.Item], str]:
    """Resolve one operation to an existing item.

    Returns (命中项, 候选列表, matched_by)。matched_by 是 exact / fuzzy / none。
    候选列表**必须**带回前端 —— 模糊匹配拿 top-1 就执行是误操作的根源:
    用户说"存入充电宝", 库里只有"充电器", 以前会直接给充电器加库存且不告诉任何人。
    """
    if op.get("item_id"):
        item = db.get(models.Item, op["item_id"])
        if item:
            return item, [item], "exact"
    name = (op.get("item_name") or "").strip()
    if not name:
        return None, [], "none"
    exact = _find_exact_item(db, name)
    if exact:
        return exact, [exact], "exact"
    # 以前是 limit=1 —— 候选本就查得到, 只是被丢掉了。
    matches = search_items(db, name, limit=5)
    if matches:
        return matches[0], matches, "fuzzy"
    return None, [], "none"


def _apply_stock_op(
    db: Session, intent: str, item: models.Item, qty: int,
    loc_id: int | None, note: str = "语音操作",
) -> models.Transaction:
    """take_out / consume / put_in on an existing item. Flushes (no commit)."""
    apply_quantity_delta(item, intent, qty, loc_id)
    tx = models.Transaction(
        item_id=item.id,
        action=intent,
        quantity=qty,
        location_id=loc_id or item.location_id,
        note=note,
    )
    db.add(tx)
    db.flush()
    return tx


def _create_or_merge_item(
    db: Session, name: str, qty: int, loc_id: int | None, note: str = "语音创建",
) -> tuple[models.Item, models.Transaction, bool]:
    """Create an item, or merge quantity into an exact name/alias match.
    Returns (item, tx, merged). Flushes (no commit)."""
    existing = _find_exact_item(db, name)
    if existing:
        existing.quantity = (existing.quantity or 0) + qty
        existing.updated_at = datetime.now()
        if loc_id:
            existing.location_id = loc_id
        tx = models.Transaction(
            item_id=existing.id,
            action="put_in",
            quantity=qty,
            location_id=existing.location_id,
            note=note + "(已合并)",
        )
        db.add(tx)
        db.flush()
        return existing, tx, True
    item = models.Item(name=name, location_id=loc_id, quantity=qty)
    db.add(item)
    db.flush()
    tx = models.Transaction(
        item_id=item.id,
        action="put_in",
        quantity=item.quantity,
        location_id=item.location_id,
        note=note,
    )
    db.add(tx)
    db.flush()
    return item, tx, False


_OP_VERB = {"find": "查找", "take_out": "取出", "put_in": "存入", "consume": "用完",
            "create_item": "新增", "delete_item": "删除"}

# ---------------------------------------------------------------------------
# 待确认方案 (plan) —— 只解析不落库
#
# 为什么要有这一层: 老流程是"边解析边落库", 而 put_in/take_out/consume 在 item_id
# 缺失时会退化成 `candidates_objs[0]` —— 兜底查询用的还是**整句话**。于是
# "把新买的洗发水放进浴室柜子" 会命中"洗手液"并直接给它加库存, 用户只看到一句
# 汇总话术, 完全不知道加错了人头。plan 把"AI 认为是谁"变成"AI 建议是谁 + 全部候选",
# 由用户在界面上逐条确认。模糊命中一律**不预选已有物品**, 默认按新物品处理。
# ---------------------------------------------------------------------------

# 允许"建一条新物品"的意图。取出/用完一个不存在的东西没有意义。
ALLOW_NEW_INTENTS = {"put_in", "create_item"}
# 方案里可选项的 key: "new" = 新建, "skip" = 跳过, "i:<id>" = 已有物品。
OPT_NEW = "new"
OPT_SKIP = "skip"


def _opt_key(item_id: int) -> str:
    return f"i:{item_id}"


def parse_option_key(key: str) -> tuple[str, int | None]:
    """把前端回传的 key 解析成 (kind, item_id)。"""
    key = (key or "").strip()
    if key == OPT_NEW:
        return OPT_NEW, None
    if key == OPT_SKIP:
        return OPT_SKIP, None
    if key.startswith("i:"):
        return "item", _coerce_int(key[2:])
    return OPT_SKIP, None


def _same_name_items(db: Session, name: str) -> list[models.Item]:
    """名称或别名与 name 完全一致的全部物品 (同名可能分布在多个位置)。"""
    name_lower = (name or "").strip().lower()
    if not name_lower:
        return []
    out: list[models.Item] = []
    for it in _all_items(db):
        if (it.name or "").lower() == name_lower or name_lower in _alias_set(it.aliases or ""):
            out.append(it)
    return out


def _option_dict(it: models.Item, kind: str, score: float | None) -> dict[str, Any]:
    loc = location_path(it.location) if it.location else "未指定位置"
    return {
        "key": _opt_key(it.id),
        "kind": kind,
        "item_id": it.id,
        "label": it.name,
        "sublabel": f"{loc} ×{it.quantity}",
        "score": score,
    }


def _do_find(db: Session, op: dict[str, Any]) -> dict[str, Any]:
    """只读查询一条 op。plan 与批量执行共用 —— find 不改数据, 没必要让用户确认。"""
    item, cands, how = _find_item_for_op(db, op)
    r: dict[str, Any] = {
        "intent": "find", "item_id": None, "item_name": op.get("item_name"),
        "quantity": op["quantity"], "executed": False, "transaction_id": None,
        "speech": "", "candidates": _cand_dicts(cands), "matched_by": how,
        "pending": False, "location_path": None, "remaining": None,
        "location_options": [],
    }
    if not item:
        r["speech"] = f"没找到{op.get('item_name') or '该物品'}"
        return r
    loc = location_path(item.location) if item.location else "未登记位置"
    r.update(item_id=item.id, item_name=item.name, executed=True,
             location_path=location_path(item.location) if item.location else None,
             remaining=item.quantity)
    r["speech"] = f"{item.name}在{loc}(×{item.quantity})"
    return r


def _plan_one(db: Session, op: dict[str, Any]) -> dict[str, Any]:
    """给一条 op 算出候选选项和预选项。不碰数据库写入。"""
    intent = op["intent"]
    name = (op.get("item_name") or "").strip()
    allow_new = intent in ALLOW_NEW_INTENTS
    force_new = bool(op.get("force_new")) and allow_new

    # LLM 直接点名的那个 (可能既不在 exact 也不在 fuzzy 里)。先查它, 因为下面可能要借它的名字。
    llm_item = db.get(models.Item, op["item_id"]) if op.get("item_id") else None

    # 模型偶尔只给 item_id 不给 item_name。这时没有名字可用来"新建", 只能沿用它点的那条 ——
    # 不兜住的话会预选 "新建" 但名字是空的, 前端确认按钮永远点不下去。
    # 必须在算 exact/fuzzy **之前**补上, 否则拿空名字去查, 同名判定必然落空。
    borrowed_name = False
    if not name and llm_item is not None:
        name = llm_item.name or ""
        borrowed_name = True

    exact_items = _same_name_items(db, name) if name else []
    exact_ids = {it.id for it in exact_items}
    fuzzy_items = [it for it in search_items(db, name, limit=5) if it.id not in exact_ids] if name else []
    if llm_item is not None and llm_item.id not in exact_ids and llm_item not in fuzzy_items:
        fuzzy_items.insert(0, llm_item)

    options: list[dict[str, Any]] = []
    new_opt = {
        "key": OPT_NEW, "kind": "new", "item_id": None,
        "label": f"新建「{name or '未命名'}」", "sublabel": "", "score": None,
    }
    if allow_new and force_new:
        options.append(new_opt)
    for it in exact_items:
        options.append(_option_dict(it, "exact", 1.0))
    for i, it in enumerate(fuzzy_items):
        options.append(_option_dict(it, "fuzzy", round(max(0.05, 0.6 - i * 0.1), 2)))
    if allow_new and not force_new:
        options.append(new_opt)

    # 预选 + 理由。这里的取舍是本功能的核心: **模糊命中绝不预选已有物品。**
    if not name:
        # 既没名字也没 id, 什么都干不了。
        selected, matched_by = OPT_SKIP, "none"
        reason = "没识别出物品名"
    elif borrowed_name and llm_item is not None:
        # 名字是从 item_id 反查来的 —— 用户根本没说物品名(比如"把它放回书桌1"),
        # 那就没有"新建"的余地, 直接用模型点的那条。
        selected, matched_by = _opt_key(llm_item.id), "exact"
        reason = f"按上下文认成「{name}」"
    elif force_new:
        selected, matched_by = OPT_NEW, "created"
        reason = "说了新增 → 默认建新"
    elif llm_item is not None and not exact_items and not allow_new:
        # 只有 id 没同名记录, 且这条不能新建 (取出/用完) —— 就用模型点的那条, 但明说是猜的。
        selected, matched_by = _opt_key(llm_item.id), "fuzzy"
        reason = f"库里没有「{name}」, 这是最接近的一条"
    elif len(exact_items) == 1:
        selected, matched_by = _opt_key(exact_items[0].id), "exact"
        reason = "名称一致"
    elif len(exact_items) > 1:
        selected, matched_by = _opt_key(exact_items[0].id), "ambiguous"
        reason = f"{len(exact_items)} 处同名, 请选"
    elif allow_new:
        selected, matched_by = OPT_NEW, "created"
        reason = ("库里没有, 默认建新" if not fuzzy_items
                  else "库里没有; 下面几个只是名字相近")
    elif fuzzy_items:
        selected, matched_by = _opt_key(fuzzy_items[0].id), "fuzzy"
        reason = "只有名字相近的, 请复核"
    else:
        selected, matched_by = OPT_SKIP, "none"
        reason = f"库里没有「{name or '该物品'}」"

    loc_id, loc_ambig = _resolve_location(db, op)
    loc = db.get(models.Location, loc_id) if loc_id else None
    if loc_ambig:
        reason += f"; 位置「{op.get('location_name')}」有 {len(loc_ambig)} 个候选, 请选一个"
    return {
        "intent": intent,
        "item_id": None if selected in (OPT_NEW, OPT_SKIP) else parse_option_key(selected)[1],
        "item_name": name,
        "quantity": op["quantity"],
        "executed": False,
        "transaction_id": None,
        "speech": "",
        "candidates": _cand_dicts(exact_items + fuzzy_items),
        "matched_by": matched_by,
        "pending": True,
        "location_id": loc_id,
        "location_name": op.get("location_name"),
        "location_path": location_path(loc) if loc else None,
        "location_options": [
            {"location_id": l.id, "name": l.name, "path": location_path(l)}
            for l in loc_ambig
        ],
        "remaining": None,
        # 方案专属字段
        "options": options,
        "selected": selected,
        "allow_new": allow_new,
        "force_new": force_new,
        "new_name_default": name,
        "reason": reason,
    }


def plan_operations(
    db: Session, text: str, ops: list[dict[str, Any]], base: dict[str, Any]
) -> dict[str, Any]:
    """把一串 op 变成"待确认方案"。只读, 不落库。"""
    # find 是只读的, 当场查掉当作信息行展示; 只有会改数据的才进"待确认"。
    planned = [_do_find(db, op) if op["intent"] == "find" else _plan_one(db, op)
               for op in ops]
    base["stage"] = "plan"
    base["plan_id"] = str(uuid.uuid4())
    base["operations"] = planned
    base["needs_confirmation"] = True
    base["executed"] = False
    intents = {o["intent"] for o in ops}
    base["intent"] = ops[0]["intent"] if len(intents) == 1 else "batch"
    # 方案阶段的候选给前端做 3D 高亮用: 汇总各条的候选。
    seen: set[int] = set()
    merged_cands: list[dict[str, Any]] = []
    for r in planned:
        for c in r["candidates"]:
            if c["item_id"] in seen:
                continue
            seen.add(c["item_id"])
            merged_cands.append(c)
    base["candidates"] = merged_cands
    todo = [r for r in planned if r["pending"]]
    found = [r["speech"] for r in planned if not r["pending"] and r["speech"]]
    if len(todo) > 1:
        base["speech"] = f"识别到 {len(todo)} 个操作, 请在屏幕上确认后执行"
    elif len(todo) == 1:
        one = todo[0]
        base["speech"] = (f"要{_OP_VERB.get(one['intent'], '执行')}"
                          f"{one['item_name'] or '这个物品'}×{one['quantity']} 吗? 请确认")
    else:
        base["speech"] = "; ".join(found) or "没有需要执行的操作"
    if found and todo:
        base["speech"] = "; ".join(found) + " —— " + base["speech"]
    return base


def _create_item_forced(
    db: Session, name: str, qty: int, loc_id: int | None, note: str = "语音新增",
) -> tuple[models.Item, models.Transaction]:
    """建一条全新物品, **不做同名合并**。

    用户说"新增 X 到 Y"就是要一条新档案 —— 哪怕库里已经有同名的。
    合并需求由方案界面上的候选项承担 (选已有物品即为合并)。
    """
    item = models.Item(name=name, location_id=loc_id, quantity=qty)
    db.add(item)
    db.flush()
    tx = models.Transaction(
        item_id=item.id, action="put_in", quantity=qty,
        location_id=item.location_id, note=note,
    )
    db.add(tx)
    db.flush()
    return item, tx


def apply_operations(
    db: Session, text: str, decisions: list[dict[str, Any]], base: dict[str, Any]
) -> dict[str, Any]:
    """执行用户确认过的方案。**单事务** —— 要么全成要么全不动。

    decisions 每条: {intent, option_key, new_item_name, location_id, location_name, quantity}
    option_key: "new" / "skip" / "i:<item_id>"
    """
    results: list[dict[str, Any]] = []
    fragments: list[str] = []
    mutated = False
    for d in decisions:
        intent = d.get("intent") or "put_in"
        if intent not in MUTATION_INTENTS:
            continue
        qty = max(1, _coerce_int(d.get("quantity")) or 1)
        kind, item_id = parse_option_key(d.get("option_key") or "")
        loc_id, loc_ambig = _resolve_location(db, d)
        r: dict[str, Any] = {
            "intent": intent, "item_id": item_id,
            "item_name": (d.get("new_item_name") or "").strip() or None,
            "quantity": qty, "executed": False, "transaction_id": None,
            "speech": "", "candidates": [], "matched_by": "", "pending": False,
            "location_path": None, "remaining": None, "location_options": [],
        }
        if kind == OPT_SKIP:
            r["speech"] = f"已跳过{_OP_VERB.get(intent, intent)}{r['item_name'] or '这一条'}"
            results.append(r)
            fragments.append(r["speech"])
            continue
        # 注意顺序: 用户明确选了 skip 的条目在上面就返回了 —— 他都说不执行了,
        # 位置明不明确无关紧要, 回"位置不明确"会让人以为是系统没搞定。
        if loc_ambig:
            # 位置没能唯一确定 —— 宁可这条不落库, 也不要猜一个位置糊弄过去。
            r["speech"] = f"位置「{d.get('location_name')}」不明确, 没执行"
            r["pending"] = True
            r["location_options"] = [
                {"location_id": l.id, "name": l.name, "path": location_path(l)}
                for l in loc_ambig
            ]
            results.append(r)
            fragments.append(r["speech"])
            continue

        if kind == OPT_NEW:
            name = (d.get("new_item_name") or "").strip()
            if not name:
                r["speech"] = "新物品缺少名称, 这条没执行"
                results.append(r)
                fragments.append(r["speech"])
                continue
            item, tx = _create_item_forced(db, name, qty, loc_id)
            mutated = True
            loc_text = location_path(item.location) if item.location else "未指定位置"
            r.update(item_id=item.id, item_name=item.name, executed=True,
                     transaction_id=tx.id, matched_by="created")
            r["speech"] = f"已新增{item.name}×{qty} 到{loc_text}"
        else:
            item = db.get(models.Item, item_id) if item_id else None
            if item is None:
                r["speech"] = "目标物品已不存在, 这条没执行"
                results.append(r)
                fragments.append(r["speech"])
                continue
            r["item_name"] = item.name
            r["matched_by"] = "confirmed"
            if intent == "delete_item":
                # cascade 会连带删掉该物品的全部历史流水, 且不可恢复。
                # 界面上已经二次确认过了, 这里如实执行。
                name = item.name
                db.delete(item)
                db.flush()
                mutated = True
                r.update(executed=True)
                r["speech"] = f"已永久删除{name}"
                results.append(r)
                fragments.append(r["speech"])
                continue
            # create_item 但用户挑了已有物品 => 意思是合并进去, 等价于 put_in。
            action = "put_in" if intent == "create_item" else intent
            tx = _apply_stock_op(db, action, item, qty, loc_id, note="语音操作(已确认)")
            mutated = True
            r.update(executed=True, transaction_id=tx.id)
            if action == "put_in":
                loc_text = location_path(item.location) if item.location else "原位置"
                r["speech"] = f"已存入{item.name}×{qty}到{loc_text}(共{item.quantity})"
            else:
                r["speech"] = f"已{_OP_VERB[action]}{item.name}×{qty}(剩{item.quantity})"
            r["location_path"] = location_path(item.location) if item.location else None
            r["remaining"] = item.quantity
        results.append(r)
        fragments.append(r["speech"])

    if mutated:
        db.commit()

    tx_ids = [r["transaction_id"] for r in results if r["transaction_id"]]
    base["stage"] = "applied"
    base["operations"] = results
    base["executed"] = bool(tx_ids)
    base["transaction_id"] = tx_ids[0] if tx_ids else None
    base["needs_confirmation"] = False
    base["confidence"] = 1.0
    intents = {r["intent"] for r in results}
    base["intent"] = results[0]["intent"] if len(intents) == 1 and results else "batch"
    base["speech"] = "; ".join(f for f in fragments if f) or "没有需要执行的操作"
    return base



def _execute_batch(
    db: Session, ops: list[dict[str, Any]],
    cfg: AppConfig, base: dict[str, Any],
) -> dict[str, Any]:
    """Execute a multi-operation utterance ("把手表、铅笔、橡皮放进书桌1") atomically-ish:
    each op resolves + executes independently; one commit at the end.
    Supports every intent type: find is read-only, the rest mutate."""
    threshold = cfg.voice.confidence_threshold
    has_mutation = any(o["intent"] in MUTATION_INTENTS for o in ops)
    if has_mutation and base["confidence"] < threshold:
        base["needs_confirmation"] = True
        base["pending_action"] = {"intent": "batch", "operations": ops}
        if not base["speech"]:
            descs = []
            for o in ops:
                name = o.get("item_name")
                if not name and o.get("item_id"):
                    it = db.get(models.Item, o["item_id"])
                    name = it.name if it else None
                descs.append(f"{_OP_VERB[o['intent']]}{name or '物品'}×{o['quantity']}")
            base["speech"] = f"我不太确定, 要执行这{len(ops)}个操作吗: " + ", ".join(descs)
        return base

    op_results: list[dict[str, Any]] = []
    fragments: list[str] = []
    cand: list[dict[str, Any]] = []
    mutated = False
    for op in ops:
        intent = op["intent"]
        qty = op["quantity"]
        r: dict[str, Any] = {
            "intent": intent,
            "item_id": op.get("item_id"),
            "item_name": op.get("item_name"),
            "quantity": qty,
            "executed": False,
            "transaction_id": None,
            "speech": "",
            "candidates": [],
            "matched_by": "",
            "pending": False,
            "location_path": None,
            "remaining": None,
            "location_options": [],
        }
        item: models.Item | None = None
        if intent == "find":
            r = _do_find(db, op)
            item = db.get(models.Item, r["item_id"]) if r["item_id"] else None
        elif intent == "delete_item":
            # 唯一不立即执行的动作。Item.transactions 是 cascade delete-orphan,
            # 真删会连带抹掉全部历史流水且无法还原, 所以只解析目标, 等前端确认。
            item, cands, how = _find_item_for_op(db, op)
            r["candidates"] = _cand_dicts(cands)
            r["matched_by"] = how
            if not item:
                r["speech"] = f"没找到{op.get('item_name') or '该物品'}"
            else:
                loc = location_path(item.location) if item.location else "未登记位置"
                r.update(item_id=item.id, item_name=item.name, pending=True)
                r["speech"] = f"要永久删除{item.name}({loc})吗?确认后不可恢复"
        elif intent == "create_item":
            name = (op.get("item_name") or "").strip()
            if not name:
                r["speech"] = "有一项缺少物品名称"
            else:
                loc_id, loc_ambig = _resolve_location(db, op)
                if loc_ambig:
                    # 位置没能唯一确定 —— 这条不落库, 等用户挑清楚了再说。
                    r["pending"] = True
                    r["matched_by"] = "none"
                    r["speech"] = f"位置「{op.get('location_name')}」不明确, 没执行"
                else:
                    if op.get("force_new"):
                        item, tx = _create_item_forced(
                            db, name, qty, loc_id, note="语音新增(批量)")
                        merged = False
                    else:
                        item, tx, merged = _create_or_merge_item(
                            db, name, qty, loc_id, note="语音创建(批量)")
                    mutated = True
                    r.update(item_id=item.id, item_name=item.name,
                             executed=True, transaction_id=tx.id)
                    r["candidates"] = _cand_dicts([item])
                    r["matched_by"] = "exact" if merged else "created"
                    if merged:
                        r["speech"] = f"已存入{item.name}×{qty}(共{item.quantity})"
                    else:
                        r["speech"] = f"已新增{item.name}×{qty}"
        else:
            item, cands, how = _find_item_for_op(db, op)
            r["candidates"] = _cand_dicts(cands)
            r["matched_by"] = how
            loc_id, loc_ambig = _resolve_location(db, op)
            if loc_ambig:
                # 位置没能唯一确定 —— 这条不落库, 等用户挑清楚了再说。
                item = None
                r["pending"] = True
                r["matched_by"] = "none"
                r["speech"] = f"位置「{op.get('location_name')}」不明确, 没执行"
                r["location_options"] = [
                    {"location_id": l.id, "name": l.name, "path": location_path(l)}
                    for l in loc_ambig
                ]
            elif not item:
                name = (op.get("item_name") or "").strip()
                if intent == "put_in" and name:
                    if op.get("force_new"):
                        # "新增/新建" 这类措辞明说了是新东西 —— 照建不误。
                        item, tx, _merged = _create_or_merge_item(
                            db, name, qty, loc_id, note="语音存入(新建)")
                        mutated = True
                        r.update(item_id=item.id, item_name=item.name,
                                 executed=True, transaction_id=tx.id)
                        r["candidates"] = _cand_dicts([item])
                        r["matched_by"] = "created"
                        loc_text = location_path(item.location) if item.location else "未指定位置"
                        r["speech"] = f"库里没有{item.name}, 已新建×{qty}放到{loc_text}"
                    else:
                        # 不许静默建档 —— 与 plan 路径一致: 没明说是新东西就先问。
                        r["pending"] = True
                        r["matched_by"] = "none"
                        r["speech"] = f"库里没有{name}, 要新建吗"
                        op_results.append(r)
                        fragments.append(r["speech"])
                        continue
                else:
                    r["speech"] = f"没找到{name or '该物品'}"
            else:
                tx = _apply_stock_op(
                    db, intent, item, qty, loc_id, note="语音操作(批量)")
                mutated = True
                r.update(item_id=item.id, item_name=item.name,
                         executed=True, transaction_id=tx.id)
                if intent == "put_in":
                    loc_text = location_path(item.location) if item.location else "原位置"
                    r["speech"] = f"已存入{item.name}×{qty}到{loc_text}(共{item.quantity})"
                else:
                    r["speech"] = f"已{_OP_VERB[intent]}{item.name}×{qty}(剩{item.quantity})"
        if item is not None:
            r["location_path"] = location_path(item.location) if item.location else None
            r["remaining"] = item.quantity
        if r["executed"] and item is not None:
            cand.append({
                "item_id": item.id,
                "item_name": item.name,
                "location_path": location_path(item.location) if item.location else None,
                "score": 1.0 - (len(cand) * 0.05),
            })
        op_results.append(r)
        fragments.append(r["speech"])
    if mutated:
        db.commit()

    failed = [r for r in op_results if not r["executed"]]
    tx_ids = [r["transaction_id"] for r in op_results if r["transaction_id"]]
    base["operations"] = op_results
    base["executed"] = bool(tx_ids)
    base["transaction_id"] = tx_ids[0] if tx_ids else None
    intents = {o["intent"] for o in ops}
    base["intent"] = ops[0]["intent"] if len(intents) == 1 else "batch"
    if cand:
        base["candidates"] = cand
    # If anything failed, the LLM's cheerful speech would over-claim — override
    # with the truthful per-op aggregate. Pure-find batches always use the
    # per-item aggregate so every location gets read out.
    if not base["speech"] or failed or not has_mutation:
        base["speech"] = "; ".join(fragments)
    return base


def _single_op(
    intent: str, item: models.Item | None, qty: int, *,
    executed: bool = False, tx_id: int | None = None, pending: bool = False,
    matched_by: str = "", candidates: list[dict[str, Any]] | None = None,
    speech: str = "",
) -> dict[str, Any]:
    """把单条(非批量)操作也表述成一条 operation 记录。

    前端因此只需要一套渲染与改判逻辑, 不用为单条/批量分叉。
    """
    return {
        "intent": intent,
        "item_id": item.id if item else None,
        "item_name": item.name if item else None,
        "quantity": qty,
        "executed": executed,
        "transaction_id": tx_id,
        "speech": speech,
        "candidates": candidates if candidates is not None else (_cand_dicts([item]) if item else []),
        "matched_by": matched_by,
        "pending": pending,
        "location_path": location_path(item.location) if (item and item.location) else None,
        "remaining": item.quantity if item else None,
        "location_options": [],
    }


def _candidate_objects(db: Session, ids: list[int], fallback_query: str) -> list[models.Item]:
    if ids:
        items = db.query(models.Item).filter(models.Item.id.in_(ids)).all()
        order = {i: idx for idx, i in enumerate(ids)}
        items.sort(key=lambda x: order.get(x.id, 999))
        return items
    return search_items(db, fallback_query, limit=5)


def execute_intent(
    db: Session, text: str, parsed: dict[str, Any], cfg: AppConfig,
    *, plan_only: bool = False,
) -> dict[str, Any]:
    """Materialize the parsed intent. Returns the IntentResult-shaped dict.

    plan_only=True 时**任何会改数据的操作都不执行**, 只返回待确认方案
    (见 plan_operations)。只读意图 (find/list/assist) 不受影响。
    网页端在 voice.confirm_before_apply 打开时走这条; 群机器人没有界面可点, 从不走。
    """
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
        "operations": [],
        "stage": "direct",
        "plan_id": None,
        "raw": parsed,
    }

    ops = _normalize_operations(parsed, text)
    if parsed.get("_dropped_ops"):
        # 以前这里是静默 continue —— 用户说了四件事只做成三件也毫无提示。
        app_log.warning("AI 返回了 %d 条无法识别的操作, 已忽略: %r",
                        len(parsed["_dropped_ops"]), parsed["_dropped_ops"][:3])
    if not ops:
        # 单条操作时模型走顶层字段而不填 operations —— 合成一条, 让下游只有一套逻辑。
        single = _op_from_parsed(parsed)
        if single:
            ops = [single]

    has_mutation = any(o["intent"] in MUTATION_INTENTS for o in ops)
    if ops and has_mutation and plan_only:
        # 待确认方案: 单条和多条走**完全同一条路**。
        # 老代码在 len(ops)==1 时折回顶层单条路径, 而那条路径的行为和批量路径不一致
        # (批量里 put_in 找不到物品会自动新建, 单条却会挂到模糊候选上), 是一整类 bug 的来源。
        return plan_operations(db, text, ops, base)
    if len(ops) >= 2:
        return _execute_batch(db, ops, cfg, base)
    if len(ops) == 1:
        # Model put a single op into the array — fold it into the top-level fields
        # and continue down the normal single-op path.
        op = ops[0]
        intent = base["intent"] = parsed["intent"] = op["intent"]
        for key in ("item_id", "item_name", "location_id", "location_name"):
            if op.get(key) is not None:
                parsed[key] = op[key]
        parsed["quantity"] = op["quantity"]
        parsed["force_new"] = bool(op.get("force_new"))

    # Resolve a location name ("书桌1") to an id for the single-op path.
    loc_ambig: list[models.Location] = []
    if not parsed.get("location_id") and parsed.get("location_name"):
        parsed["location_id"], loc_ambig = _resolve_location(db, parsed)

    # 位置有歧义就不能悄悄丢掉继续写库 —— 以前这里用 `_` 把候选丢了, location_id
    # 变成 None, create_item/put_in/take_out/consume 照样 commit, 话术还说"已存入
    # 到未指定位置", 听起来像用户没说位置, 而不是"你说的位置有歧义"。
    if loc_ambig and intent in {"take_out", "put_in", "consume", "create_item"}:
        base["needs_confirmation"] = True
        base["pending_action"] = {
            "intent": intent,
            "item_id": parsed.get("item_id"),
            "item_name": parsed.get("item_name"),
            "location_name": parsed.get("location_name"),
            "location_options": [
                {"location_id": l.id, "name": l.name, "path": location_path(l)}
                for l in loc_ambig
            ],
            "quantity": int(parsed.get("quantity") or 1),
        }
        base["speech"] = (
            f"位置「{parsed.get('location_name')}」不明确, "
            f"有 {len(loc_ambig)} 个候选, 没执行")
        return base

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
            # find 也补一条 operation。schemas.IntentOperationResult 的约定是
            # "单条语句也填, 让前端只有一套渲染逻辑" —— 以前只有批量 find 填了,
            # 单条 find 是空的, 前端那块区域就莫名其妙地空着。
            base["operations"] = [_single_op(
                "find", top, 1, executed=True,
                matched_by="exact" if parsed.get("item_id") else "fuzzy",
                candidates=base["candidates"] or None, speech=base["speech"],
            )]
        else:
            base["speech"] = speech or "暂未找到这种东西,可能还没登记进来"
            base["operations"] = [_single_op(
                "find", None, 1, matched_by="none", speech=base["speech"],
            )]
            base["operations"][0]["item_name"] = (
                parsed.get("item_name") or "").strip() or None
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

        # 用户说"新增/新建 X" 时不做任何合并 —— 他要的就是一条新档案。
        # 其余情况(模型自己选了 create_item)沿用同名合并, 避免重复档案。
        if parsed.get("force_new"):
            item, tx = _create_item_forced(db, name, qty, loc_id)
            merged = False
        else:
            item, tx, merged = _create_or_merge_item(db, name, qty, loc_id)
        db.commit()
        db.refresh(tx)
        base["executed"] = True
        base["transaction_id"] = tx.id
        loc_text = location_path(item.location) if item.location else "未指定位置"
        if merged:
            base["speech"] = speech or f"已找到同名物品,已将数量+{qty},现在{item.name}共{item.quantity}个,位置:{loc_text}"
        else:
            base["speech"] = speech or f"记下啦,{name}放在{loc_text}了"
        base["operations"] = [_single_op(
            "create_item", item, qty, executed=True, tx_id=tx.id,
            matched_by="exact" if merged else "created", speech=base["speech"],
        )]
        return base

    if intent == "delete_item":
        # 唯一不立即执行的动作 —— Item.transactions 是 cascade delete-orphan,
        # 真删会连带抹掉全部历史流水且无法还原。只解析目标, 等前端确认。
        item_id = parsed.get("item_id")
        if not item_id and candidates_objs:
            item_id = candidates_objs[0].id
        item = db.get(models.Item, item_id) if item_id else None
        if not item:
            base["intent"] = "unknown"
            base["speech"] = speech or "没找到要删除的物品"
            return base
        loc_text = location_path(item.location) if item.location else "未登记位置"
        base["speech"] = f"要永久删除{item.name}({loc_text})吗?确认后不可恢复"
        base["operations"] = [_single_op(
            "delete_item", item, 1, pending=True,
            matched_by="exact" if parsed.get("item_id") else "fuzzy",
            candidates=base["candidates"] or None, speech=base["speech"],
        )]
        return base

    if intent in {"take_out", "put_in", "consume"}:
        item_id = parsed.get("item_id")
        if not item_id and candidates_objs:
            item_id = candidates_objs[0].id
        if not item_id:
            name = (parsed.get("item_name") or "").strip()
            if intent == "put_in" and name:
                # 不许静默建档 —— 与批量路径 (_execute_batch) 一致: 没明说是新东西就先问,
                # 单条(这里)和多条不能一个默默新建一个不建, 表现必须一样。
                qty = int(parsed.get("quantity") or 1)
                base["speech"] = speech or f"库里没有{name}, 要新建吗"
                base["operations"] = [_single_op(
                    intent, None, qty, pending=True, matched_by="none",
                    candidates=base["candidates"] or None, speech=base["speech"],
                )]
                base["operations"][0]["item_name"] = name
                return base
            base["intent"] = "unknown"
            base["speech"] = speech or "没找到这个物品,要不要先创建一个"
            return base
        item: models.Item | None = db.get(models.Item, item_id)
        if not item:
            base["intent"] = "unknown"
            base["speech"] = "物品不存在了"
            return base
        qty = int(parsed.get("quantity") or 1)
        tx = _apply_stock_op(db, intent, item, qty, parsed.get("location_id"))
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
        # LLM 直接给了 item_id 才算 exact; 否则是从搜索结果里取的 top-1 —— 标 fuzzy,
        # 前端会打"猜的"角标提示用户复核。
        base["operations"] = [_single_op(
            intent, item, qty, executed=True, tx_id=tx.id,
            matched_by="exact" if parsed.get("item_id") else "fuzzy",
            candidates=base["candidates"] or None, speech=base["speech"],
        )]
        return base

    # unknown
    if not base["speech"]:
        base["speech"] = "没听懂呢,能换个说法吗"
    return base
