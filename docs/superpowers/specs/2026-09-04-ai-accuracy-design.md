# AI 准确性与群机器人确认 (子项目 B)

日期: 2026-09-04
状态: 待用户评审

前序: 子项目 A (性能/功耗, 已完成) → D (文档/CI, 已完成) → **B (本文)** → C (UX 收敛)。

## 背景

评测台现有 32 条 case 全绿, 但覆盖面有洞: `delete_item` **零 case**, 且 `run_eval.py:155` 只跑到 `plan_only=True` —— 用户确认之后的 `apply_operations` 从未被评测过。全绿不等于准, 只等于"测到的都对"。

调研确认仍然存在的弱点 (file:line 均为当前分支):

| 弱点 | 位置 | 现象 |
|---|---|---|
| 位置名子串误命中 | `llm/intent.py:353-368` | 无精确匹配时取**第一个**子串命中且无歧义反馈。说"书桌1"库里只有"书桌10"→ 静默落到书桌10 |
| 两条执行路径不一致 | `intent.py:1006-1021` vs `1198-1215` | 非 plan 路径 `put_in` 找不到就**自动新建**("语音存入(自动新建)"); 单条路径挂到模糊 top-1 或要求确认 |
| 多物品句 force_new 归属 | `intent.py:297-300` | 句级兜底只在 `len(operations)==1` 时生效, "新增 A 和 B 到 C" 无法判定修饰谁 |
| 量词 | `intent.py:238, 289` | `max(1, ...)` 把"一打""若干"压成 1 |
| 否定句 | `SYSTEM_PROMPT` 全文 | "别拿螺丝刀"无任何处理 |
| 群机器人无确认 | `dingtalk.py:158`、`telegram.py:132`、`feishu.py:278` | 三处**复制粘贴**地把 confidence 抬到 1.0, 静默落库 |

## 已确认的取舍 (用户拍板)

1. **群机器人只对高风险操作要确认** —— 删除、模糊命中已有物品、一句话改 3 条以上; 其余 (明确新建、精确命中的存取) 维持秒回。
2. **位置歧义时不猜** —— 精确名优先; 无精确名且多个候选时列出来让用户选, 与现有"模糊命中绝不预选已有物品"的设计一致。
3. 范围含四项: 补评测缺口 / 位置匹配 + 两条路径统一 / 多物品 force_new 归属 / 量词与否定句。

## 设计

### B0. 先补评测, 再改代码

顺序是硬性的: 每一项修复都必须先有能复现问题的 case (改前红), 改完变绿。否则无法证明"改准了"而不是"改成了另一种错"。

`eval/cases.py` 新增分类与 case:
- `delete`(3): 单条删除、批量删除、删除不存在的物品
- `apply`(4): 端到端 plan → decisions → apply。需要 `run_eval.py` 支持新字段 `decisions` 与期望的落库结果 `after`(物品数量/是否存在/位置)
- `locambig`(3): "书桌1" vs "书桌10"、多个子串命中、精确名优先
- `forcenew_multi`(2): "新增 A 和 B 到 C" 两条都该 force_new
- `quantifier`(3): "一打""两双""若干"
- `negation`(2): "别拿螺丝刀"、"除了电池其它都拿走"

`run_eval.py` 扩展: `score_case` 支持 `after` 断言 (apply 之后查库校验), 报告新增"落库对"一列。`--replay` 的 cassette 需要为新 case 录制。

### B1. 位置匹配 (`_resolve_location`)

改签名为返回 `(loc_id | None, ambiguous: list[Location])`:
1. 精确名匹配 (大小写不敏感) → 直接返回, 无歧义
2. 无精确名 → 收集**全部**子串命中 (名字或全路径)
3. 恰好 1 个 → 返回它
4. 多个 → 返回 `(None, 候选列表)`

调用方处理:
- `_plan_one`: 把候选放进该条 operation 的新字段 `location_options[]`, `selected` 留空, `reason` 说明"位置有多个候选"
- `apply_operations`: `location_id` 必须已定; 未定且有候选 → 该条报错不落库
- 非 plan 路径: 视为未解析, 走确认

**排序**: 候选按 (名字长度差, 全路径字典序) 排, 让"书桌1"的候选里"书桌10"排在"书桌100"前面 —— 仅影响展示顺序, 不影响"不猜"的原则。

### B2. 两条执行路径统一

`_execute_batch` 的 `put_in` 找不到物品时不再静默 `_create_or_merge_item`。新规则 (与 plan 路径一致):
- `force_new=true` → 新建 (不变)
- 否则 → 该条标记 `pending=true` + `matched_by="none"`, 附候选, 不落库

这**改变了群机器人的行为**: 以前"把不存在的东西放进X"会静默建档, 现在会回一句"库里没有X, 要新建吗"。这正是本子项目要的。

`_apply_stock_op` 的库存增减逻辑目前在 `intent.py:527`、`revise.py:145`、`items.py:371` **手抄三份**(A 阶段刻意没动)。本次收敛到 `services/inventory.py` 一份, 三处引用 —— 否则"两条路径统一"只是表面。

### B3. 多物品句的 force_new 归属

不靠正则猜"新增"修饰谁 —— 那是 LLM 该做的判断。三层:
1. `SYSTEM_PROMPT` 补一条明确规则 + 一个多物品示例, 要求逐条设 `force_new`
2. `TOOLS` schema 里 `force_new` 的 description 强调"每条操作各自判断"
3. 后端兜底放宽: 句子明确是新增措辞 (`_looks_force_new`) 且**没有任何一条** op 自己设了 `force_new` 时, 对**全部** `put_in` op 置 `force_new=true`(现在只在单条时生效)

第 3 层是保守的: 只要 LLM 表达了意图 (哪怕只给一条设了), 就不覆盖它。

### B4. 量词与否定

**量词**: 新增 `_QUANTIFIER_MAP`(打=12, 双/对=2, 沓/摞按 1 处理并记日志), 在 `_normalize_operations` 里解析 `quantity` 为空但 item_name 前后有量词的情况。**"半"不做分数** —— 数量是整数列, "半瓶洗手液"仍记 1 (在 prompt 与文档里写明这个取舍)。

**否定**: prompt 加规则 (否定的物品不要出现在 operations 里) + 后端兜底: `_NEGATION_HINTS`("别""不要""除了""不用")命中时, 若 LLM 仍返回了被否定物品的 op, 丢进 `_dropped_ops` 并在 speech 里说明。兜底只做"整句否定单个物品"这种明确情形, 复杂的条件句 (如"如果没有就买") 不在本次范围。

### B5. 群机器人分级确认

**风险判定** (新函数 `services/risk.py::plan_risk(operations) -> tuple[bool, str]`):
高风险 = 满足任一:
- 含 `delete_item`
- 任一条 `matched_by == "fuzzy"`(模糊命中已有物品)
- 变更类 op 数量 ≥ 3
- 任一条位置歧义未决 (B1 的产物)

**待确认存储** (新模块 `services/pending.py`): **进程内内存字典 + TTL**, 不落 DB。
- 键: `(channel, chat_id, sender_id)` —— 同一个人在同一个群里同时只能有一个待确认方案, 新方案覆盖旧的。三端的具体字段 (**其中两处需要新接线**):
  | 渠道 | chat_id | sender_id | 现状 |
  |---|---|---|---|
  | 钉钉 | `payload["conversationId"]` | `payload["senderStaffId"]` | `conversationId` **当前完全没被读取**, 需新增提取 |
  | Telegram | `message["chat"]["id"]` | `message["from"]["id"]` | 两者都已提取 (`telegram.py:88,90`) |
  | 飞书 | `message.chat_id` | `event.sender.sender_id.open_id` | `sender_id` 已提取但**没往下传**(`_handle_async` 签名只收 chat_id), 需加参数 |
- 值: `{plan: dict, created_at: float}`, 方案是 `plan_operations` 的返回体 (已确认全部是 JSON 原生类型)
- TTL 300 秒, 惰性清理 + 容量上限 200 (照抄 `feishu._seen_message_ids` 的现成模式)
- **重启即丢**: 可接受 —— 超过几分钟的待确认方案本来就该失效; 文档里写明

**确认词识别**: 复用前端 `useVoice.js` 的 `YES_WORDS`/`NO_WORDS` 语义, 但在后端实现 (`services/pending.py::classify_reply`), 不跨端共享代码。只认明确的确认/取消词; 认不出就当作**新的一句话**去解析 (不要把"再拿个螺丝刀"误当确认)。

**三条链路的接入**: 抽公共函数 `services/botflow.py::handle_bot_message(channel, chat_id, sender_id, text, cfg) -> str`, 内含: 查待确认 → 是确认词就 apply → 否则 parse → 判风险 → 高风险存 pending 并回方案文本 / 低风险直接执行。三个 handler 各自只负责收发消息与身份提取。这同时消掉了三处复制粘贴的 confidence 强抬。

**钉钉的特殊性**: 它是同步 webhook, 无法主动推消息 (`outgoing_webhook` 配置项存在但零代码使用, 本次**不启用**)。所以钉钉的"方案已过期"不会主动通知 —— 用户回"确认"时若已过期, 就在那次回复里说明。Telegram/飞书同样按此处理 (不实现主动过期通知), 保持三端一致。

**消息渲染**: `telegram._format_reply` 与 `dingtalk._format_markdown_response` 目前完全不读 `operations`/`options`/`stage`。新增 `services/botfmt.py::format_plan(plan) -> str`, 逐条列出"序号. 意图 物品 ×数量 → 位置"并附一行"回复 确认 执行, 取消 放弃"。飞书发的是纯文本 (`msg_type="text"`), 所以格式化必须在**无 Markdown** 下也可读 —— 用序号和空格, 不依赖星号。

## 测试

- 后端 unittest 新增: `test_location_ambiguity.py`(B1)、`test_bot_confirm_flow.py`(B5: 高低风险分流、TTL 过期、确认词识别、三端共用 handler)、扩充 `test_intent_plan.py`(B2/B3/B4)
- eval: 新增 17 条 case, 全部录 cassette 支持 `--replay`
- **改前基线已存档**: `.superpowers/sdd/2026-09-03-perf-power-cleanup/eval-baseline-before-B.txt`(32 条 100%)。B 完成后新增 case 必须绿, 原 32 条不得回退

## 明确不做

- 钉钉主动推送 (`outgoing_webhook`) —— 需要公网回调, 另开话题
- 飞书富卡片 (post/interactive) —— 纯文本够用, 卡片是 C 的事
- 分数数量 ("半瓶") —— 数量列是整数, 不改数据模型
- 复杂条件句 ("如果没有就…")、指代消解 ("把它放回去")、多轮上下文
- iPad 语音的确认流程 —— 已有 PlanReview, 不动
- 前端任何改动
