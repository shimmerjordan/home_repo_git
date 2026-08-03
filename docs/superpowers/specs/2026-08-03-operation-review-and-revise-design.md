# 操作逐条复核与改判 — 设计

日期: 2026-08-03
状态: 待实施

## 背景

语音说"存入充电宝",系统模糊匹配到库里已有的相似物品(比如"充电器"),直接给它加了库存 ——
用户既看不到匹配了谁,也没法说"不对,我要存的是个新东西"。

根因在两个函数的匹配强度不一致:

| 函数 | 匹配方式 | 后果 |
|---|---|---|
| [`_find_item_for_op`](../../../backend/app/llm/intent.py#L299) | `search_items(name, limit=1)` 模糊 top-1 | 抓到相似物品直接改它的库存 |
| [`_create_or_merge_item`](../../../backend/app/llm/intent.py#L337) | `_find_exact_item` 精确名/别名 | 这个是对的 |

`put_in`/`take_out`/`consume` 都走前者。匹配过程被完全吞掉。

雪上加霜的是:后端 [`IntentOperationResult`](../../../backend/app/schemas.py#L125) 早就逐条返回了
每个操作的结果(含 `transaction_id`),但 [VoicePanel.vue](../../../frontend/src/components/VoicePanel.vue)
**零处引用 `operations`** —— 批量操作时用户只能看到一句汇总 speech。

## 目标

1. 每条操作结果显式写出"我操作的是哪个物品、在哪、匹配是精确还是猜的"
2. 任意单条操作(含批量里的某一条)可撤销,并改判到正确目标 —— 别的已有物品,或"其实是新物品"
3. 语音支持删除物品

## 核心决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 执行时机 | **默认仍按匹配立即执行**,事后改判 | 用户明确要求;绝大多数匹配是对的,不该每次都拦一道 |
| 撤销语义 | **硬回滚,当作没发生** | 用户是来纠错的,错误不该留在流水里。撤销行为写审计日志 |
| 改判入口 | **默认只显示命中项,点开才列候选** | 批量一次 5 条,全展开会把卡片撑爆 |
| 语音删除 | **真删,但先进待确认区** | 见下 |

### 为什么删除是唯一不立即执行的动作

[`models.py:48`](../../../backend/app/models.py#L48) 的 `Item.transactions` 带
`cascade="all, delete-orphan"` —— **删物品会连带抹掉它的全部历史流水**。而
[`delete_item`](../../../backend/app/routers/items.py#L341) 只往审计日志存了物品快照,没存流水。

所以真删之后没法"当作没发生":物品能从快照重建,但流水历史永久丢失、id 变化、3D 坐标与关联全断。
这与"撤销 = 硬回滚"直接冲突。因此语音删除只解析目标并置 `pending=True`,
在结果卡片里等一次显式确认,确认后才调用现有的 `DELETE /api/items/{id}`。

## 后端改动

### 1. 匹配层返回候选

`_find_item_for_op` 改签名:

```python
def _find_item_for_op(db, op) -> tuple[models.Item | None, list[models.Item], str]:
    """返回 (命中项, 候选列表, matched_by)。matched_by: exact | fuzzy | none"""
```

- `op["item_id"]` 命中 → `exact`,候选只有它自己
- 精确名/别名命中(`_find_exact_item`) → `exact`
- 否则 `search_items(name, limit=5)`:top-1 为命中项,**5 个全部作为候选**,标 `fuzzy`
- 都没有 → `none`

`limit=1 → 5` 是这次修复的关键一行:候选本来就查得到,只是被丢掉了。

### 2. 数据契约

`IntentOperationResult` 扩三个字段:

```python
candidates: list[IntentCandidate] = []   # 含命中项, 命中项排第一
matched_by: str = ""                     # exact | fuzzy | created | none
pending: bool = False                    # True = 未执行, 等确认 (仅 delete_item)
```

### 3. 单条操作也填 operations

现在只有 `len(ops) >= 2` 才走 `_execute_batch` 并填 `operations`。改成单条路径也写入一条记录,
前端只需一套渲染逻辑,不为单条/批量分叉。

### 4. 新增 delete_item 意图

- LLM prompt 增加该意图说明
- 加入 `MUTATION_INTENTS`(从而也进 `BATCH_INTENTS`,支持"把A和B都删了")
- 执行分支:**只解析目标,不执行**,置 `pending=True`、`executed=False`

### 5. 新端点 `backend/app/routers/revise.py`

| 端点 | body | 作用 |
|---|---|---|
| `POST /api/revise/undo` | `{transaction_id}` | 硬回滚 |
| `POST /api/revise/redirect` | `{transaction_id, target}` | **单个事务内** undo + 对新目标重执行 |

`target` 二选一:`{"item_id": 123}` 或 `{"new_item_name": "充电宝"}`。

确认删除不需要新端点 —— 直接用现有的 `DELETE /api/items/{id}`。

改判必须是服务端单事务:前端分两步调的话,中间失败会留下"撤销了但没重执行"的破损状态。

#### 硬回滚规则

| 原 action | 回滚 |
|---|---|
| `put_in` | 数量 −qty;**若该物品最早的一条流水就是这条**(判定为本次新建)→ 连物品一起删 |
| `take_out` / `consume` | 数量 +qty |
| `adjust` | **拒绝**,返回 400 |

`adjust` 撤不了是已知缺口:流水表没存操作前的值。它只有快捷操作的「用完了」按钮在用,
不在语音路径内,本轮不补。

#### 护栏:只能撤销该物品的最后一条流水

否则拒绝并提示。这不是并发检测,而是保证回滚本身有意义 —— 撤销一条陈旧流水会把数量
算成错值(例如中间飞书机器人又存入过)。判据:该 `item_id` 下 `created_at` 最大的流水就是它。

## 前端改动

### 新组件 `OperationResults.vue`

渲染 `result.operations`,每条一行:

```
✓ 已存入 充电宝 ×2  → 卧室/床头柜  (共 5)        [改判]
⚠ 已存入 充电器 ×2  → 卧室/床头柜  (共 3)  猜的   [改判]
🗑 待删除 数据线                                [确认删除] [取消]
```

- `matched_by === 'fuzzy'` 显示"猜的"角标并用琥珀色 —— 这类最可能需要改判
- `pending === true`(delete_item)显示待确认样式与两个按钮
- 「改判」点开后:候选单选(命中项预选中)+「都不是,存入新物品」+ `[取消] [撤销并改执行]`
- 「都不是,存入新物品」仅对 `put_in` / `create_item` 有意义;`take_out` / `consume` 下改为
  「都不是,取消这次操作」(纯撤销,不重执行)

### 接入

VoicePanel「最新识别结果」卡片,放在 `result.speech` 之下、候选物品之上。
成功后复用上轮已有的 `onQuickActionDone()` 统一刷新。

## 边界情况

| 情况 | 行为 |
|---|---|
| 撤销的不是该物品最后一条流水 | 400,卡片内提示"该物品之后又被操作过,无法撤销" |
| 撤销 `adjust` | 400,提示流水未记录操作前的值 |
| 改判目标就是当前命中项 | 前端禁用「撤销并改执行」按钮 |
| 撤销 put_in 且物品为本次新建 | 连物品一起删;若它已被其他流水引用则只减数量 |
| 待确认删除的物品在确认前已被删 | 404,卡片提示并移除该条 |
| 同一条 operation 重复点击 | busy 锁 |

## 验证

项目无自动化测试基础设施,不在本轮引入。验证方式:

1. `cd frontend && npm run build`
2. `docker compose up -d --build`(注意 `node:20-alpine` 需先从 daocloud 镜像源打本地 tag)
3. 手工清单:
   - 语音"存入充电宝"且库里有相似的"充电器" → 结果显示命中项 + "猜的"角标
   - 点改判 → 选「都不是,存入新物品」→ 原物品数量还原,新物品建出来
   - 批量"存入A和B" → 两条独立结果,各自可改判
   - 撤销一条 take_out → 数量加回,流水里那条消失
   - 语音"把X删了" → 显示待删除,不立即执行;点确认才消失
   - 对一条陈旧流水撤销(先用飞书再动一次该物品)→ 明确报错

## 改动文件

| 文件 | 改动 |
|---|---|
| `backend/app/schemas.py` | `IntentOperationResult` 扩 3 字段 |
| `backend/app/llm/intent.py` | 匹配层返回候选、单条填 operations、delete_item 意图、prompt |
| `backend/app/routers/revise.py` | 新增 |
| `backend/app/main.py` | 注册路由 |
| `frontend/src/api.js` | `undoTx` / `redirectTx` |
| `frontend/src/components/OperationResults.vue` | 新增 |
| `frontend/src/components/VoicePanel.vue` | 接入 |
