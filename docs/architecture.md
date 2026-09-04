# 架构与项目结构

## 运行时拓扑

```
        iPad Safari (https://nas:8443)
                 │
                 ▼
    ┌────────────────────────────┐
    │  容器 storage-app           │   ← entrypoint.sh 先签证书, 再 exec supervisord 带起下面两个进程
    ├────────────────────────────┤
    │  nginx (80 / 443, 自签证书) │   ← 唯一对外端口
    │  ├─ /          静态 Vue 3   │
    │  ├─ /ca.crt    本地 CA      │   ← iPad 装这个
    │  └─ /api/, /docs → 反代     │
    │              │ 同容器回环   │   ← 127.0.0.1:8000, 不走 docker network
    ├──────────────┴─────────────┤
    │  FastAPI 127.0.0.1:8000     │   ← 不暴露到容器外
    │  ├─ /api/items, /locations  │
    │  ├─ /api/voice/intent       │
    │  ├─ /api/voice/transcribe ──┼──► Whisper :9000 (独立容器 storage-whisper, 可选 profile)
    │  ├─ /api/revise/undo|redirect│
    │  ├─ /api/dingtalk/webhook   │ ← 钉钉 inbound
    │  ├─ /api/settings           │
    │  ├─ /api/diag, /api/logs    │
    │  ├─ /api/backup/* ──────────┼──► 你配置的 WebDAV (可选, 定时/手动)
    │  ├─ summary + intent + 置信度
    │  ├─ OpenAI 兼容 client ─────┼──► 你配置的任何 LLM URL
    │  ├─ Telegram poller ────────┼──► api.telegram.org (outbound)
    │  └─ Feishu WS supervisor ───┼──► open.feishu.cn (outbound, lark-oapi)
    └──────────────┬─────────────┘
                   │ 唯一挂载: ./data → /app/data
        ./data/storage.db (SQLite)
        ./data/config.json (运行时配置)
        ./data/logs/       (后端日志)
        ./data/certs/      (本地 CA + 服务器证书)
```

前后端原先是 `storage-frontend` + `storage-backend` 两个容器,现在合成一个 `storage-app`:
省掉一次跨容器 HTTP、少一份镜像、日志在一处。Whisper 仍是独立容器 —— 它镜像大、启动慢、
按 profile 可选,且后端靠 docker DNS 名 `whisper` 找它,**服务名不能改**。

## 上下文摘要(防 token 爆炸)

不会把整个仓库 dump 给 LLM。检索打分在 [`backend/app/services/inventory.py`](../backend/app/services/inventory.py)
的 `search_items()`,组装 prompt 在 [`backend/app/services/summary.py`](../backend/app/services/summary.py),策略:

1. 中英混合分词器(2/3 字 CJK rolling shingle + ASCII 词)
2. 用 token 对所有物品的 `name / aliases / category / tags` 做 ILIKE OR 预筛
3. 加权打分:名称×3 / 别名×2.5 / 分类×1.2 / 标签×1.0 + 长度奖励
4. **多物品语句先按并列连接词切分**(顿号/逗号/"和""与""以及""还有""顺便"...),整句 +
   每个分段各自检索、按 id 去重合并(总量上限 40)。原因:长句整句检索时,句子里的多个
   物品会在 Top-N 里互相挤名额,库存一多某个物品就可能挤不进候选、LLM 拿不到它的 id
5. Top-30(极速模式 12)候选 + 完整位置树 + 最近 8 条流水(极速模式跳过) + 分类直方图 +
   **库存=0 的旧档案**(最多 15 件,只在补货场景给 LLM 当 `put_in` 目标,find/assist 不引用,
   避免"再买了两瓶水"新建重复档案)
6. "需求"型问句(如"我发烧了")自动放宽到完整库存清单(最多 80 件)

## 项目结构

顶层目录:`backend/app`(FastAPI 后端)、`frontend/src`(Vue 3 前端源码)、
`deploy/`(仅镜像内用到的 nginx/supervisor/entrypoint 配置)、`docs/`(本目录)。
构建产物由根 `Dockerfile` 打进单一镜像,细节见 [`docs/deployment.md`](deployment.md)。

**不再维护逐文件的目录树** —— 之前这里是一棵 80+ 行的树,新增文件/组件后没人记得
回来同步,长期与代码脱节。改成下面两张一句话职责表:文件名会变、会加会删,
但"这个 router/service 是干什么的"这句话很少变,维护成本低很多。

### 后端 `routers/`(URL 前缀 → 职责)

| 文件 | 前缀 | 职责 |
|---|---|---|
| `items.py` | `/api/items`, `/api/transactions` | 物品 CRUD、CSV 导入导出、`/depleted` 待补充列表、单品流水;`recent_router` 挂同文件,给全局流水筛选 + `/pending-returns` 待归位 |
| `locations.py` | `/api/locations` | 位置树 CRUD(home / room / 容器 / 家具) |
| `voice.py` | `/api/voice` | `/intent`(解析/出方案)、`/apply`(执行确认过的方案)、`/transcribe`(Whisper 代理) |
| `revise.py` | `/api/revise` | `/undo` 硬回滚一条流水、`/redirect` 撤销后改目标重做 |
| `settings.py` | `/api/settings` | 运行时配置读写(脱敏)、`/test-llm` 连通性测试 |
| `diag.py` | `/api/diag`, `/api/logs` | 诊断快照、后端日志增量拉取、前端日志上报 |
| `audit.py` | `/api/audit` | 审计日志查询(Git-blame 风格) |
| `dingtalk.py` | `/api/dingtalk` | 钉钉自定义机器人 inbound webhook(加签校验)+ liveness check |
| `backup.py` | `/api/backup` | WebDAV 备份配置、连通性测试、手动触发、备份列表/下载/删除、从远端或上传文件恢复 |

### 后端 `services/`(纯逻辑,不含路由)

| 文件 | 职责 |
|---|---|
| `inventory.py` | 物品/位置 CRUD 辅助 + 关键词搜索(CJK 分词器 + 加权打分,见下节) |
| `summary.py` | 组装给 LLM 的上下文摘要(分段检索候选 + 位置树 + 流水 + 分类直方图) |
| `audit.py` | 审计日志 diff / log / serialize |
| `secrets.py` | 敏感字段脱敏(API key / token / 密码打码),`settings` 与 `backup` 路由共用 |
| `logbuffer.py` | 内存环形缓冲日志,供 `/api/logs` 增量拉取 |
| `telegram.py` | Telegram 长轮询 worker(关闭态挂在 `asyncio.Event` 上零唤醒,见下节) |
| `feishu.py` | 飞书 Stream Mode WebSocket supervisor(重连状态机 + 关闭态零唤醒,见下节) |
| `backup.py` | WebDAV 定时备份调度器(关闭态零唤醒)+ 备份包打包/加密/恢复,恢复后复用 `migrations.run_all` |

`llm/client.py` 是 OpenAI 兼容 HTTP client(工具调用 + JSON 兜底),`llm/intent.py` 是意图解析 +
置信度阈值 + 执行,`migrations.py` 是启动与备份恢复共用的 SQLite 迁移(见「启动顺序」)。

前端 `components/` 是 Vue 单文件组件(语音面板、3D 场景、物品/位置管理、设置、备份、审计等),
`composables/` 是可复用逻辑(语音原语、音频分析、撤销栈、可暂停轮询、跨页共享的物品/位置数据源等)。
同样不逐文件列举 —— 直接看目录比维护一份必然过期的清单可靠。

## 数据模型要点

- **Location** 是无限深度的树。`kind` 区分行为(`home` / `room` / `cabinet` / ...);`parent_id IS NULL` 是根。
- **顶层"家"**(`kind='home'`)是逻辑分组,无 3D mesh。同一台 NAS 可以管多个家(我家 / 老家 / 父母家),3D 页有家切换器。
- **Item.location_id** 指向叶子(可以是房间、收纳箱、抽屉的某一层等)。
- **Transaction** 记录每次 `take_out / put_in / adjust`,可追溯。
- **AuditLog** 记录所有 mutation 的 before/after diff,字段级。

## 启动顺序

容器级(`deploy/entrypoint.sh` → `deploy/supervisord.conf`):

1. `/app/data/certs` 里已有 `ca.key` + `ca.crt` 就**直接复用**,没有才生成 —— iPad 上装过的信任靠这个不失效
2. 按当前 `LAN_IP` 重签服务器证书(SAN 里带上局域网 IP),把 `ca.crt` 拷到 web 根目录供下载
3. `exec supervisord`:`backend` (priority 10) 比 `nginx` (priority 20) 先 spawn,但 supervisord 不等前一个就绪,所以后端 bind 8000 之前 `/api` 会有几秒 502(静态页正常);任一进程挂了自动重拉

后端进程内(`backend/app/main.py`):

4. `Base.metadata.create_all` — SQLAlchemy 建表
5. `migrations.run_all(engine)`([`backend/app/migrations.py`](../backend/app/migrations.py),启动与
   WebDAV 备份恢复共用同一套逻辑):
   - `ensure_columns()` — SQLite ALTER 补缺失列(`geometry` / `uuid` / `pos_x` / `pos_z`)、回填 UUID
   - `ensure_indexes()` — 补 `transactions` 按 `item_id` / `location_id` 的索引(老库建表时没有)
   - `migrate_to_home()` — 一次性数据迁移:无 `kind='home'` 但有 root 位置时,创建"我家"并把所有 root 改 parent
6. FastAPI 启动钩子:`telegram.start()` + `feishu.start()` + `backup.start()` 各起一个异步常驻任务
   (功能是否开启不影响任务是否创建,见下面「三个后台循环关闭态零唤醒」)

## 性能与功耗设计取舍

以下两处是本次(2026-09)性能优化改的地方,也是将来最容易被"顺手优化"掉的地方 ——
写在这里说明**为什么**,免得以后有人看不懂就把它改回简单但费电的写法。

### Scene3D 按需渲染,而不是常驻 RAF

[`frontend/src/components/Scene3D.vue`](../frontend/src/components/Scene3D.vue) 同时被首页
`VoicePanel.vue` 和 3D 页 `BuildingPanel.vue` 各实例化一份 —— 以前 `loop()` 是常驻
`requestAnimationFrame`,两个 WebGL 场景 60fps 空转是整个项目最大的功耗源。

现在是"活跃窗口"模型:交互 / 相机 tween / 高亮脉冲 / 数据重建都调 `wake()`,把
`activeUntil` 往后推 `IDLE_AFTER_MS`(1.5s);最后一次活跃过后停帧。三个门禁
(`pageVisible` 页面可见 / `inView` 容器在视口 / `activated` 未被 keep-alive 换出)
任一关闭时 `canRender()` 为 false,`wake()` 直接不生效。

两个坑,改这段代码前必须知道:

1. **`inLoop` 重入锁**:`controls.update()` 在返回前会**同步**派发 `change` 事件,而
   `loop()` 一开头就把 `raf` 置 0 —— 如果不挡住重入,`change` 监听里的 `wake()` 会在
   同一帧里再排一次 `requestAnimationFrame`,而且是指数级增长,`stopLoop()` 也取消不掉
   这些孤儿 RAF。所以 `loop()` 执行期间 `inLoop=true`,`wake()` 见到 `inLoop` 为真时只
   延长 `activeUntil`,续排交给 `loop()` 尾部统一处理。
2. **异步回调里改场景必须自己 `wake()`**:比如高亮遮罩 5 秒后的自动恢复——如果这段
   代码只改了场景图(材质/可见性)却不调 `wake()`,当时 RAF 循环大概率已经因为超时停帧,
   于是改动永远不会被画出来,界面看着像没生效。
3. **不变量**:`highlightItemIds` 的 watcher 用 `join(',')` 指纹(不是 `deep:true`)判断
   变化。连续两次搜索**同一个**物品时,指纹不变、watcher 不触发 —— 靠的是既有代码
   "先清空 `highlightItemIds` 再 `setTimeout` 重新设置"的复位动作,才能在第二次搜索时
   再次触发高亮。**谁要是去掉那个复位 dance,重复搜索同一物品会静默失效**(不报错,
   只是没反应)。

### Telegram / 飞书 / 备份三个后台循环:关闭态零唤醒

`services/telegram.py`、`services/feishu.py`、`services/backup.py` 各有一个常驻
`asyncio` 循环。功能关闭(或飞书/备份处于不需要动作的状态)时,循环不再周期性
`wait_for(..., timeout=N)` 空醒检查配置,而是无 timeout 挂在 `asyncio.Event` 上 ——
只有 [`routers/settings.py`](../backend/app/routers/settings.py) 的 PATCH(或备份的
`reload()`)调用 `.set()` 才会唤醒它。开启态的节奏不变:Telegram 长轮询 `timeout=25`,
飞书 supervisor 巡检间隔 `POLL_INTERVAL_S=15`,备份调度器 60s 一次心跳。

`start()` 里那个 `asyncio.Event()` **只在真正新建 task 的分支里创建**,不会跨调用复用
旧对象 —— 沿用旧 Event 可能绑在上一个(已经关闭的)event loop 上,下次 `.wait()`
会抛 `RuntimeError: got Future attached to a different loop`(测试里每个用例都是独立的
`asyncio.run()`,这个坑很容易在测试里先暴露出来)。
