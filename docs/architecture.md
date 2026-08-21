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
    │  ├─ /api/dingtalk/webhook   │ ← 钉钉 inbound
    │  ├─ /api/settings           │
    │  ├─ /api/diag, /api/logs    │
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

不会把整个仓库 dump 给 LLM。[`backend/app/services/summary.py`](../backend/app/services/summary.py) 的策略:

1. 中英混合分词器(2/3 字 CJK rolling shingle + ASCII 词)
2. 用 token 对所有物品的 `name / aliases / category / tags` 做 ILIKE OR 预筛
3. 加权打分:名称×3 / 别名×2.5 / 分类×1.2 / 标签×1.0 + 长度奖励
4. Top-30 候选 + 完整位置树 + 最近 8 条流水 + 分类直方图
5. "需求"型问句(如"我发烧了")自动放宽到完整库存清单(最多 80 件)

## 项目结构

```
repo_git/
├── start.sh                          # 一键启动/停止/日志
├── docker-compose.yml                # app (前后端合一) + whisper (可选 profile)
├── Dockerfile                        # 多阶段: node 打前端 → python 装后端 + nginx + supervisor
├── deploy/                           # 只在镜像里用到的运行时配置
│   ├── entrypoint.sh                 # CMD: 先签本地 CA/服务器证书, 再 exec supervisord
│   ├── supervisord.conf              # 一个容器带两个进程: uvicorn + nginx
│   ├── nginx.conf                    # 80 / 443 两个 server + TLS 证书路径
│   └── app-routes.conf               # 两个 server 共用的路由, /api 反代 127.0.0.1:8000
├── README.md                         # 入口 + 链到 docs/
├── docs/                             # 模块化文档
│   ├── architecture.md               # 本文档
│   ├── deployment.md                 # 部署、首次配置、故障排查
│   ├── voice.md                      # 语音状态机、LLM、iOS 注意事项
│   ├── api.md                        # REST API + 数据持久化
│   ├── changelog.md                  # 版本演进
│   └── bots/                         # 群机器人接入
│       ├── README.md                 # 总览 + 对比表
│       ├── dingtalk.md
│       ├── telegram.md
│       └── feishu.md
│
├── backend/
│   ├── requirements.txt
│   └── app/
│       ├── main.py                   # FastAPI 入口 + access log + 启动钩子
│       ├── config.py                 # 持久化的运行时配置 (LLM / Voice / Bot)
│       ├── database.py
│       ├── models.py                 # SQLAlchemy: Item / Location / Transaction / AuditLog
│       ├── schemas.py                # Pydantic 输入输出模型
│       ├── llm/
│       │   ├── client.py             # OpenAI-compatible HTTP client (tool 调用 + JSON 兜底)
│       │   └── intent.py             # 意图解析 + 置信度阈值 + 执行
│       ├── services/
│       │   ├── inventory.py          # CRUD + 关键词搜索 + CJK 分词器
│       │   ├── summary.py            # 给 LLM 的上下文摘要
│       │   ├── audit.py              # 审计日志 diff/log/serialize
│       │   ├── logbuffer.py          # 内存环形缓冲日志
│       │   ├── telegram.py           # Telegram 长轮询 worker
│       │   └── feishu.py             # 飞书 Stream Mode WebSocket supervisor
│       └── routers/
│           ├── items.py              # CRUD / CSV 导入导出 / 流水筛选
│           ├── locations.py
│           ├── voice.py              # /intent + /transcribe (Whisper 代理)
│           ├── settings.py
│           ├── diag.py               # /diag /logs /logs/client
│           ├── audit.py              # 审计日志查询
│           └── dingtalk.py           # 钉钉 inbound webhook
│
└── frontend/                         # 纯前端源码, 构建产物由根 Dockerfile 打进镜像
    ├── vite.config.js
    ├── tailwind.config.js
    ├── postcss.config.js
    ├── package.json
    ├── index.html
    ├── public/manifest.webmanifest
    └── src/
        ├── main.js
        ├── style.css
        ├── App.vue                   # 顶部导航 + hash 路由 (#tab=xxx)
        ├── api.js                    # 所有 fetch 封装
        ├── composables/
        │   ├── useVoice.js           # SR 原语: wake / capture / yes-no / speak
        │   ├── useAudioMeter.js      # AudioContext + Analyser → 频谱
        │   ├── useEditHistory.js     # 撤销栈
        │   ├── useClientLog.js       # 全局错误捕获 + 上报后端
        │   ├── sceneLayout.js        # 家具目录 + 几何计算 + 世界坐标
        │   └── furnitureMesh.js      # 漂亮 3D 家具组合 mesh
        └── components/
            ├── VoicePanel.vue        # 状态机 UI: idle/command/confirm/processing + 3D 高亮
            ├── Waveform.vue
            ├── ItemList.vue          # 树筛 + 表格 + 备注 + 导入导出
            ├── ItemEditor.vue
            ├── LocationManager.vue   # Finder 风格文件夹浏览器
            ├── LocationTree.vue
            ├── LocationTreeNode.vue
            ├── BuildingPanel.vue     # 3D 标签:树 + 2D 平面图 + 3D 视图 + 属性 + 多家切换
            ├── PlanEditor.vue        # 2D SVG 平面图,拖拽家具、锁定模式
            ├── Scene3D.vue           # Three.js 3D 场景,家具 mesh、相机推进、高亮
            ├── LevelSlotFields.vue   # 多层容器的层/槽位编辑
            ├── TransactionFeed.vue   # 流水筛选 + 统计 + 导出
            ├── AuditPanel.vue        # Git-blame 风格审计日志
            ├── SettingsPanel.vue     # LLM / 语音 / 钉钉 / Telegram / 飞书 配置
            └── LogsPanel.vue         # 诊断 + 自检按钮 + 合并日志查看器
```

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
5. `_ensure_columns()` — SQLite mini-migration,补缺失列、回填 UUID
6. `_migrate_to_home()` — 一次性数据迁移:无 home 但有 root 位置时,创建"我家"并把所有 root 改 parent
7. FastAPI 启动钩子:`telegram.start()` + `feishu.start()` 异步常驻
