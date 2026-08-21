# 六项优化设计 — 飞书稳定性 / 单容器 / 人工确认 / 回撤 / 强制新建 / AI 准确度

日期: 2026-08-21
状态: 已批准, 实施中

用户提的六项:

1. 飞书长连接跑久了卡死 (`3001 registered too many conns` 刷屏)
2. 前后端两个 docker 合并成一个 (前端端口不变, 数据兼容)
3. 「最新识别结果」不再直接落 AI 结论, 每个单品用可下拉/滚动的候选区选择, 含可自填项
4. 「近期取放记录」加回撤按钮
5. 说「新增 X 到 Y」= 全新物品, 不做匹配
6. 深度优化 AI 准确度 (多物品操作经常不符合预期)

---

## 一、飞书长连接卡死

### 根因 (读 `lark_oapi/ws/client.py` 1.4.15 源码确认)

三个互相叠加的缺陷:

**(a) 我们的 `_stop_ws()` 是空操作。** 它尝试 `setattr(client, "_stop"/"stopped"/"_should_stop", True)`
和 `client.stop()/close()` —— lark 的 `Client` **这四个属性/方法全都没有**。所以旧 WS 客户端永远
停不下来, 而 `_stop_ws()` 却把 `_ws_thread = None` 了 —— supervisor 从此**丢失对旧线程的引用**,
认为"没在跑", 于是再起一条。多个 Client 实例同时在各自的重连循环里, 每轮都向飞书注册一条新连接
→ 服务端连接数超限 → 握手后立刻被 3001 关闭 → 再重连 → 日志里那串一秒好几条的 conn_id。

**(b) supervisor 的存活判据是"线程活着", 不是"连接活着"。**
`lark.Client.start()` 最后一句是 `loop.run_until_complete(_select())`, 而 `_select()` 是
`while True: await asyncio.sleep(3600)` —— **线程永远不退出**, 哪怕连接早就死透了。
更致命的是 `_try_connect()` 遇到 `ClientException` (连接数超限就是这一类) 会**向上抛**,
穿过 `_reconnect()`、穿过 `_receive_message_loop()` 的 except 块, 变成一个没人接的 task 异常 ——
**内部重连链就此彻底断掉**, 而线程仍然活着。supervisor 看到 `is_alive() == True`, 走
`pass  # all good — lark's internal reconnect is handling things` 分支, **永远不重启**。
这就是"卡死": 机器人静默失联, 只能重启容器。

**(c) `Client._connect()` 泄漏锁。**
```python
await self._lock.acquire()
if self._conn is not None:
    return            # ← 没 release
```
一旦走到这个 return, `_disconnect()` / `_write_message()` 之后全部永久阻塞在
`self._lock.acquire()` 上 —— lark 那条线程彻底僵死。

另外 `loop` 是 lark **模块级全局变量**(import 时绑定), 所有 Client 实例共用。第二条线程里
`start()` 调 `loop.run_until_complete()` 时那个 loop 正被第一条线程跑着, 直接
`RuntimeError: This event loop is already running`。现有代码"在线程里建 stdlib loop"的
workaround 只在**首次** import 发生于该线程时有效, 重启场景下失效。

### 方案: 把重连主权从 lark 收回到我们自己手里

1. **`auto_reconnect=False`** 建 Client —— 彻底关掉 lark 那个不可控的内部重连循环。
   连接一断, `_receive_message_loop` 直接 raise, `self._conn` 变 None, 不再自己偷偷重连。
2. **自建并持有 event loop**, 不依赖 lark 的模块全局。
   `_run_ws_client` 里建 loop 后**显式覆写 `lark_oapi.ws.client.loop = my_loop`**,
   保证每条新线程用的是自己的 loop, 重启可靠。
3. **真正能停**: 停止 = `loop.call_soon_threadsafe(loop.stop)`。这会让
   `run_until_complete(_select())` 返回, `start()` 返回, 线程自然退出。
   停之前先 `run_coroutine_threadsafe(client._disconnect())` 把连接干净关掉(让飞书侧连接数回落)。
   再顺手把泄漏的 `_lock` 强行释放(`client._lock` 已锁就 release), 防 (c)。
4. **存活判据改成"连接活着"**: 新增 `_health()` —— `client._conn is not None and not client._conn.closed`。
   supervisor 每 15s 检查; 连接不健康持续 `>DEAD_AFTER_S (90s)` 就判死 → 停线程 → 按退避重启。
   同时记录 `_last_event_at`(收到任何事件时刷新), 仅作诊断展示, 不参与判死(长期没人说话是正常的)。
5. **连接数超限专属退避**: 抓到消息里含 `too many conns` / `exceed` 的异常, 退避直接跳到
   **5 分钟起, 上限 30 分钟** —— 飞书侧连接计数需要时间衰减, 30s 级别的重试只会把坑挖深。
   普通失败仍是 30s→60s→120s→300s。
6. **单飞保证**: 模块级 `threading.Lock` 包住"停旧的 + 起新的", 并且起新线程前
   `join(timeout=10)` 旧线程。任何时刻最多一条 `feishu-ws` 线程。
7. **日志止血**: lark 的 `logger` 是 `logging.getLogger("lark")`。给它挂一个
   **限流 filter** —— 同一条 message 模板 60s 内最多放过 1 条, 其余计数后丢弃, 每 60s
   汇总一行 `(同类日志已抑制 N 条)`。避免 3001 刷屏把 logbuffer 打爆(现有注释说过这会拖慢每个 API 请求)。
8. **诊断可见**: `GET /api/diag` 增加 `feishu` 段: `enabled / connected / conn_id /
   uptime_s / last_event_at / consecutive_failures / next_retry_in_s / suppressed_logs`。
   卡死不再只能靠翻日志发现。

### 验收
- 单元测试(不联网): 用 fake lark client 驱动 supervisor, 断言
  (i) 连接假死 90s 后会重启; (ii) 任意时刻只有一条线程; (iii) too-many-conns 走长退避;
  (iv) `_stop_ws` 之后线程真的退出。

---

## 二、前后端合并成单容器

### 目标约束
- 对外端口**不变**: `${HTTP_PORT:-8080}:80`, `${APP_PORT:-8443}:443`
- 数据**兼容**: `./data/storage.db` / `./data/config.json` / `./data/logs` / `./data/certs`
  路径与内容一律不动 —— 特别是 `data/certs` 里的本地 CA, 换了 iPad 就得重装, 绝不能重新生成。
- whisper 仍是独立容器(第三方镜像, 无法合并), 服务名保持 `whisper` 以便
  `whisper_url = http://whisper:9000` 继续可用。

### 镜像结构 (根目录新 `Dockerfile`, 三段式)
```
FROM node:20-alpine AS webbuild      # 编译前端 dist
FROM python:3.11-slim AS runtime     # 装 nginx + supervisor + python 依赖
  ├─ /app/app            后端代码
  ├─ /usr/share/nginx/html  前端 dist
  ├─ nginx.conf / app-routes.conf
  ├─ /entrypoint.sh      生成证书(逻辑照搬现 frontend/entrypoint.sh)
  └─ supervisord.conf    管 nginx + uvicorn 两个进程
```

**进程管理选 supervisord** 而不是 shell 里 `&` + `wait`: 任一进程崩了能单独重启并各自留日志。
考虑到本项目最现实的故障就是后端卡住(见第一项), 单独重启后端而不动 nginx 是有价值的。

- nginx 反代目标: `http://backend:8000` → **`http://127.0.0.1:8000`**
- 证书目录: `/etc/nginx/certs` → **`/app/data/certs`** (宿主机同一份文件, CA 不变)
- 新文件落在 `deploy/`: `deploy/supervisord.conf`、`deploy/entrypoint.sh`、
  `deploy/nginx.conf`、`deploy/app-routes.conf`。`frontend/` 下的 nginx/entrypoint/Dockerfile 删除。
- `docker-compose.yml`: `backend` + `frontend` 两个 service 合成一个 `app`
  (`container_name: storage-app`), whisper 不动。
- `start.sh` 加 `--remove-orphans`, 让旧的 `storage-backend` / `storage-frontend` 容器被清掉。
- 健康检查: `curl -f http://127.0.0.1:8000/api/health`。

### 代价 (明确记下来)
前端改一行也要重建整个镜像(含 pip 层)。缓存顺序安排成 requirements → npm → 源码,
让日常源码改动只重跑最后两层。

### 验收
`./start.sh --whisper` 后:
`curl http://127.0.0.1:8080/api/health` = 200、`curl -k https://127.0.0.1:8443/` 返回首页、
`data/certs/ca.crt` 的 fingerprint 与升级前**完全一致**、物品数与升级前一致。

---

## 三、识别结果先确认再落库 (核心改动)

### 现状问题
后端 `execute_intent` 是**边解析边落库**。`put_in/take_out/consume` 在
`item_id` 缺失时会 `candidates_objs[0].id` —— 而 `candidates_objs` 的兜底是
`search_items(db, 整句话, limit=5)`。于是"把新买的洗发水放进浴室柜子"会命中"洗手液"
并**直接给它加库存**。这正是用户说的"存入新物品时错误匹配到已有物品"。

### 新流程: plan → 用户改 → apply

`POST /api/voice/intent`, `context.plan_only = true` 时**只解析不落库**, 返回:

```jsonc
{
  "stage": "plan",                  // plan | applied | direct
  "plan_id": "uuid",
  "intent": "batch", "confidence": 0.9, "speech": "...",
  "operations": [{
    "intent": "put_in",
    "item_name": "洗发水", "quantity": 1,
    "location_id": 5, "location_name": "洗漱柜", "location_path": "我家 / 卫生间 / 洗漱柜",
    "executed": false, "pending": true,
    "matched_by": "fuzzy",
    "selected": "new",              // 预选项的 key
    "allow_new": true,
    "new_name_default": "洗发水",
    "reason": "库里没有同名物品, 默认按新物品处理",
    "options": [                    // 前端下拉/滚动区的数据源
      {"key":"new",    "kind":"new",   "item_id":null, "label":"新建「洗发水」", "sublabel":"我家 / 卫生间 / 洗漱柜", "score":null},
      {"key":"i:3",    "kind":"fuzzy", "item_id":3,    "label":"洗手液",         "sublabel":"我家 / 卫生间 / 洗漱柜 · 现有 1", "score":0.42}
    ]
  }]
}
```

`POST /api/voice/apply`:
```jsonc
{ "text":"...", "plan_id":"uuid",
  "operations":[{"intent":"put_in","item_id":null,"new_item_name":"洗发水",
                 "location_id":5,"location_name":null,"quantity":1,"skip":false}] }
```
→ **单事务**执行全部未 skip 的 op, 返回 `stage:"applied"` + 每条的 `executed/transaction_id`。

规则:
- 只有 mutation (`take_out/put_in/consume/create_item/delete_item`) 进 plan。
  `find/list/assist/unknown` 是只读的, 照旧立刻返回 (`stage:"direct"`) —— 查东西还要点一下确认是折磨。
- `options` 组成: 精确同名/别名 → 同名但在别的位置 → 模糊命中(带分数) → `new` (仅
  `put_in`/`create_item` 提供)。**始终附带 `new`**, 这就是用户要的"可以自己填的那一项":
  前端选中 `new` 时旁边的输入框可编辑, 默认填 `new_name_default`, 用户可改成任意名字。
- `selected` 的预选逻辑:
  - `matched_by == "exact"` → 选那个 item
  - `force_new`(见第五项) → 选 `new`
  - `matched_by == "fuzzy"` → **选 `new`**(如果 allow_new), 否则选 top-1 但 `reason` 里
    写明"仅名称相近, 请复核"。**模糊命中绝不默认落到已有物品上** —— 这是本项的关键决定。
  - `matched_by == "none"` 且不允许新建(take_out/consume) → `selected = "skip"`, 提示物品不存在
- 开关: `voice.confirm_before_apply: bool = true` (新增配置, 设置页可关)。
  前端据此决定发不发 `plan_only`。**机器人(飞书/TG/钉钉)不走 plan**, 行为完全不变。
- 向后兼容: 旧的 `context.confirmed + pending_action` 路径保留不动。

### 前端
新组件 `PlanReview.vue` 取代「最新识别结果」里 `OperationResults` 的位置(执行后仍用
`OperationResults` 展示结果与改判):
- 每条 op 一张卡: 动作徽标 + 数量步进 + **一个 `<select>` 候选下拉**(选项 > 6 时下拉自带滚动)
  + 选中 `new` 时出现的名称输入框 + 位置下拉 + 「跳过这条」勾选
- 底部: 「✓ 全部确认执行」/「取消」; 每条旁边显示 AI 的 `reason`
- 语音链路: `voice.confirm_before_apply` 打开时, TTS 播报改为
  "识别到 N 个操作, 请在屏幕上确认" —— 不再口头逐条确认(条数多时不可用)。
  单条 mutation 时保留口头"确定/取消"的快捷路径。

---

## 四、近期记录加回撤

- `TransactionOut` 增 `undoable: bool` + `undo_note: str`。
  服务端算: `action ∈ {take_out,put_in,consume}` 且**是该物品的最后一条流水**
  (`/api/revise/undo` 现有护栏), 否则 `undoable=false` 并给出人话原因
  ("该物品之后又被动过, 无法安全撤销" / "盘点记录没存操作前的数量")。
  用一条 `GROUP BY item_id` 的聚合查询批量标注, 不做 N+1。
- 前端两处都加「↩ 回撤」: `VoicePanel` 的「近期取放记录」和「流水」页 `TransactionFeed`。
  不可撤时按钮 disabled + `title` 写原因。点了先 `confirm`, 成功后刷新。
- 新建物品产生的那条 `put_in` 被回撤时后端会连物品一起删(现有逻辑), 确认文案要说清。

---

## 五、「新增 X 到 Y」= 全新物品, 不匹配

- op 结构增 `force_new: bool`。
- LLM 侧: prompt + tool schema 增 `force_new` 字段, 说明"用户说新增/新建/添加/录入时置 true"。
- 后端兜底(不能只信 prompt):
  - `intent == "create_item"` ⇒ `force_new = True` (语义上"创建"就是新的)
  - `intent == "put_in"` 且整句里出现 `新增|新建|添加|录入|新记|新买的|买了个新的`
    且该 op 的 `item_name` 在句中出现于该动词之后 ⇒ 升级成 `create_item` + `force_new`
- `force_new` 的效果:
  - plan 流程: `selected = "new"`, `options` 里已有同名物品**仍然列出**(一键改成合并), 但不预选
  - apply 时走 `create_item_forced()` —— **不做 exact-name 合并**, 直接建新行
- 注意: 这会允许同名多行存在。这是用户要的("全新的物品不需要匹配"), 且 `find` 已有
  "同名多处"的聚合展示逻辑, 不会看不见。

---

## 六、AI 准确度深度优化

### 已实测确认的头号根因: `max_tokens=512` 截断

用真实 gateway (cc-trans / claude-opus-4-8) 复现:

| 语句 | max_tokens | stop_reason | 解析出的 operations |
|---|---|---|---|
| 把手表、铅笔、橡皮、订书机、计算器放进书桌1 | 512 | **max_tokens** | 5 (侥幸完整) |
| 同上 | 2048 | tool_use | 5 |
| 我用完了一瓶洗手液, 拿了螺丝刀和卷尺, 顺便把两个充电器放回书桌1 | 512 | **max_tokens** | **0 (全丢)** |
| 同上 | 2048 | tool_use | 4 (全对) |

`claude-opus-4-8` 默认带 thinking 块, 光思考就吃掉大半预算; 4 个操作的正常输出是 490~547 tokens,
**512 本身就不够**。而 `client.py` 从不看 `stop_reason`, 截断的 tool_use input 被当成正常结果
交给 `execute_intent` —— 于是"多物品操作经常不符合预期"。

### 修改清单

1. **截断检测**: `client.py` 检查 Anthropic `stop_reason == "max_tokens"` /
   OpenAI `finish_reason == "length"` → 抛 `LLMTruncated`。`parse_intent` 捕获后
   **用 `min(max_tokens*4, 16384)` 重试一次**, 仍截断才报错。日志记录 stop_reason。
2. **默认值**: `LLMConfig.max_tokens` 512 → **4096**, 上限 8192 → 16384。
   并且**迁移已存在的 `data/config.json`**: 载入时若 `max_tokens < 2048` 则抬到 4096 并打一行日志
   —— 不迁移的话线上那份 512 一直生效, 等于没修。
3. **强制走工具**: `tool_choice` 从 `auto` 改成指定 `submit_intent`
   (Anthropic `{"type":"tool","name":...}`, OpenAI `{"type":"function",...}`)。
   消掉"模型用散文回答 → 再发一轮 chat_json"这条既慢又不准的退路。
4. **按物品分段检索** (`summary.py`): 现在是拿**整句**去 `search_items(limit=30)`,
   多物品语句里各物品互相挤名额, 大库存时有的物品根本进不了候选 → LLM 只能瞎猜 id。
   改成: 先按 `、和与及,，以及还有顺便然后` 切分出片段, **每个片段单独检索 (limit 6)**,
   与整句检索结果合并去重。保证句子里提到的每个物品都有候选进 prompt。
5. **补货看得见**: 库存为 0 的物品当前对 LLM 完全不可见 → "再买了两瓶水"会新建重复行。
   摘要里增设独立段落「库存为0(补货可用)」, 明确标注, 仅供 `put_in` 参考。
6. **单条/批量统一走一条路**: 现在 `len(ops)==1` 会被折叠回顶层单条路径, 而单条路径与批量
   路径的行为**不一致**(批量里 `put_in` 找不到物品会自动新建, 单条却会挂到模糊候选上)。
   统一成: 所有 mutation 都经 `plan_operations()` → `apply_operations()`,
   只读意图(find/assist/list)留在顶层。这一步直接消掉一整类 bug。
7. **`_normalize_operations` 加固**: 丢弃的 op 记日志(不再静默); 未知 intent 尽量映射;
   按 `(intent,item_id,item_name,quantity,location)` 去重 —— 多 tool_call 合并时会产生重复条目。
8. **prompt 强化**: 多物品必须逐条列全 + 数量分别取值 + **不确定就把 `item_id` 留空并填
   `candidates`, 不要猜** + `force_new` 用法 + 反例清单。
9. **eval 评测台** `backend/eval/`:
   - `fixtures.py`: 造一个真实感库存(含易混对: 充电宝/充电器、洗手液/洗发水、螺丝刀/螺丝、
     两处都有的电池、库存为0的抽纸)
   - `cases.py`: ~40 条标注语句, 分类为 单物品/多物品/混合意图/易混匹配/新增/补货/查询/需求推荐
   - `run_eval.py`: 起临时 sqlite → 逐条跑 `parse_intent` + `plan_operations`(不落库) →
     比对 intent / op 数 / 每 op 的目标 / 数量 / 位置 → 输出分类准确率表 + 失败明细
   - `--record` 把真实响应存成 cassette, `--replay` 离线重跑 → CI/无网也能跑回归
   - 顺带对比 `max_tokens` 512 vs 4096、`thinking` 空 vs disabled 的准确率与延迟

### 验收
`--replay` 全绿; 真跑一次记录基线准确率, 多物品类别不低于 90%。

---

## 文件影响面

| 文件 | 项 |
|---|---|
| `backend/app/services/feishu.py` | 1 |
| `backend/app/routers/diag.py` | 1 |
| `Dockerfile`(新) `deploy/*`(新) `docker-compose.yml` `start.sh` `frontend/Dockerfile`(删) `frontend/nginx.conf`(删→deploy) `frontend/entrypoint.sh`(删→deploy) | 2 |
| `backend/app/llm/intent.py` | 3,5,6 |
| `backend/app/llm/client.py` | 6 |
| `backend/app/services/summary.py` | 6 |
| `backend/app/routers/voice.py` | 3,5 |
| `backend/app/schemas.py` | 3,4,5 |
| `backend/app/config.py` | 3,6 |
| `backend/app/routers/items.py` | 4 |
| `backend/app/services/inventory.py` | 4 |
| `frontend/src/components/PlanReview.vue`(新) | 3 |
| `frontend/src/components/VoicePanel.vue` | 3,4 |
| `frontend/src/components/TransactionFeed.vue` | 4 |
| `frontend/src/components/SettingsPanel.vue` | 3,6 |
| `frontend/src/api.js` | 3,4 |
| `backend/eval/*`(新) `backend/tests/*`(新) | 1,6 |
| `docs/*` | 全部 |
