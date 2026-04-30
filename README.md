> **License: GNU AGPL-3.0-or-later** —
> 任何人可以自由使用、修改、分发本仓库代码,**但只要分发或通过网络提供服务,
> 都必须将完整源码(含修改部分)以同样的 AGPL-3.0 协议公开**。
> 详见根目录 [`LICENSE`](LICENSE)。商业闭源使用请先联系作者获取例外授权。

# 语音仓储管家 (Voice Storage)

家庭杂物仓储管理系统,通过语音查找、存放、取出物品。前端跑在 iPad 浏览器,后端用 Docker Compose 部署在 N5105 这类工控 NAS 上,完全本地运行,只把意图解析的少量摘要文字发给可配置的 OpenAI 兼容 LLM。

---

## 功能一览

### 仓储核心
- **物品**:名称 / 别名 / 分类 / 标签 / 数量 / 单价 / 备注 / 位置
- **位置**:任意层级文件夹结构(房间 → 收纳箱 → 上层 → … 不限深度)
- **流水**:每次取出/存入/盘点都自动记录,可按物品名 / 动作 / 位置 / 时间区间筛选
- **CSV 导入导出**:模板下载、合并/追加/替换三种导入模式;CSV 中位置以 `房间 / 箱子 / 上层` 路径表达,导入时自动按层级创建缺失节点

### 语音控制(完整状态机 + 双层确认)
状态流:`idle → command → confirm-text → processing → confirm-action → speaking → idle`

1. **唤醒监听**(可选,小按钮开启)
   - 浏览器 Web Speech API 持续监听,**只检查唤醒词**(只取 final 片段、滚动 buffer ≤ 80 字、不积累 transcript、不调 AI),功耗极低
   - 命中"小库 / 小仓 / 管家"等任一唤醒词后立即停掉持续 SR(避免 iOS Safari 双 SR 冲突),TTS 播报"请说",进入指令捕获
2. **指令捕获**(主操作:大圆形麦克风按钮 ~176px)
   - 单次完整识别,8 秒超时
   - 也支持手动文字输入
3. **发送前确认(省 token)** *默认开启,可在设置中关闭*
   - 弹出黄色确认卡片显示"你说的是: XXX"
   - TTS 播报问句 + 同时启动 yes/no 单次识别
   - 你可以**口头说**"确定 / 确认 / 对 / 是 / 好 / 行 / yes / ok"或"取消 / 不对 / 算了 / no",**也可以点按钮**
   - 听不清自动循环重问
4. **AI 解析意图** → 执行
5. **低置信度二次确认** *阈值默认 0.5*
   - LLM 返回 `confidence < threshold` 且是修改性操作(取出/存入)时,先不动数据,弹卡片 + 语音播报问"是想 X 吗",同样支持口头/按钮二选一
   - 模糊匹配时给候选物品列表,可点"选这个"

### LLM 接入(完全可配置)
- OpenAI 兼容协议,任何提供 `/v1/chat/completions` 的服务都行
- 内置预设:**OpenAI / 硅基流动 / DeepSeek / Ollama / 智谱 GLM**
- 运行时改 `base_url` + `api_key` + `model` + `temperature` + `timeout`,**无需重启**
- 自动用 OpenAI 风格 `tool_calls`,模型不支持时降级为 JSON 模式
- "测试连接"按钮一键验证

### 上下文摘要(防 token 爆炸)
不会把整个仓库 dump 给 LLM。`backend/app/services/summary.py` 的策略:
1. 中英混合分词器(2/3 字 CJK rolling shingle + ASCII 词)
2. 用 token 对所有物品的 `name / aliases / category / tags` 做 ILIKE OR 预筛
3. 加权打分:名称×3 / 别名×2.5 / 分类×1.2 / 标签×1.0 + 长度奖励
4. Top-30 候选 + 完整位置树 + 最近 8 条流水 + 分类直方图

### 诊断 & 日志(出问题不再瞎猜)
- **浏览器能力自检**:secure context、mediaDevices、SpeechRecognition、speechSynthesis、AudioContext、麦克风权限状态
- **后端状态卡片**:Python 版本、DB URL、items/locations/transactions 计数、LLM 配置、Whisper 状态
- **自检按钮**:测试后端 / 测试 LLM / 测试麦克风(显示波形) / 测试语音识别 / 测试语音播报
- **统一日志视图**:前后端日志合并,自动 3s 刷新,可按 客户端/服务端/全部 来源 + 级别 + 关键字过滤
  - 后端用内存环形缓冲(1000 条),捕获 `uvicorn` / 业务事件 / 异常 stack
  - 前端 hook `window.error` / `unhandledrejection` / `console.error`,错误级别同步上报到后端

### UI 细节
- 语音页:渐变 hero + 大圆形麦克风按钮(脉冲发光)+ 实时频谱波形 + RMS 指示
- 物品页:左侧位置树侧边栏(显示子节点物品数)+ 备注内联展示
- 位置页:**Finder 风格文件夹浏览器** — 面包屑导航、双击进入、hover 浮出 重命名/改类型/移动/删除、移动时检测循环引用
- 状态机指示器:每个阶段不同颜色圆点,一眼看到现在系统在做什么

---

## 架构

```
        iPad Safari (https://nas:8443)
                 │
                 ▼
    ┌────────────────────────────┐
    │  nginx (443, 自签证书)      │   ← 唯一对外端口
    │  ├─ /          静态 Vue 3   │
    │  └─ /api/, /docs → 反代     │
    └──────────────┬─────────────┘
                   │ docker network (内部)
    ┌──────────────┴─────────────┐
    │  FastAPI :8000              │
    │  ├─ /api/items, /locations  │
    │  ├─ /api/voice/intent       │
    │  ├─ /api/voice/transcribe ──┼──► Whisper :9000 (可选 profile)
    │  ├─ /api/settings           │
    │  ├─ /api/diag, /api/logs    │
    │  ├─ summary + intent + 置信度
    │  └─ OpenAI 兼容 client ─────┼──► 你配置的任何 LLM URL
    └──────────────┬─────────────┘
                   │
        ./data/storage.db (SQLite)
        ./data/config.json (运行时配置)
        ./data/certs/      (自签证书)
```

---

## 快速启动

```bash
cd repo_git
./start.sh                 # 启动 (后台)
./start.sh --whisper       # 同时启动本地 Whisper STT 服务
./start.sh --logs          # 启动后跟随日志
./start.sh stop            # 停止
./start.sh restart         # 重启
./start.sh logs            # 跟随日志
./start.sh ps              # 查看容器状态
APP_PORT=9443 ./start.sh   # 自定义端口
```

只暴露**一个端口** `8443` (HTTPS,可改)。后端、Whisper 都仅在 docker 内网。

iPad Safari 打开 `https://<NAS-IP>:8443`:
- 第一次会有自签证书警告,点 **高级 → 继续访问** 信任即可(证书会持久化在 `./data/certs/`,以后不再重生成)
- HTTPS 是浏览器开麦克风的硬性要求,这就是为什么不暴露 HTTP

---

## 首次配置

进入 **设置** 页签:

### LLM 配置
1. 点一个预设(OpenAI / 硅基流动 / DeepSeek / Ollama / 智谱 GLM)或手填
2. 填入 API Key、调整 model
3. 点 **测试连接** — 应该返回一个简短回答证明通了
4. 不支持 tool calling 的模型(如老 Ollama)取消勾选 "支持工具调用"

### 语音配置
- 唤醒词,逗号分隔(默认 `小库,小仓,管家`)
- **置信度阈值** 默认 0.5,低于此值的修改性操作会语音确认
- **发送给 AI 前先口头确认识别文本** 默认开启,省 token
- 可选启用 Whisper(纯离线 STT,需 `--whisper` 启动)

### 添加初始数据
- 进 **位置** 页 → 新建房间 → 进入后新建子文件夹(任意深度)
- 进 **物品** 页 → 新增几件物品,或下载 CSV 模板批量导入

---

## 使用示例

### 语音(主用法)

**方式 A:开启唤醒监听**(免动手)
1. 点右上角"开启唤醒监听" — 蓝色脉冲点亮
2. 对设备说"小库,充电宝在哪?"
3. 系统播报"请说" → 进入指令模式 → 你重复或继续指令
4. 黄色卡片显示"你说的是: 充电宝在哪,确认发送吗?" → 你说"确定"
5. AI 解析 → 播报"充电宝在卧室床头柜,数量 1"

**方式 B:大麦克风按钮**(更快)
- 点中间大圆按钮 → 直接说指令 → 后续流程同上

**典型语句:**
- "充电宝在哪" — find
- "我刚拿了两个 5 号电池" — take_out 2
- "把螺丝刀放进储藏室工具箱" — put_in + 自动建位置
- "新增一瓶酱油在厨房" — create_item

### 网页(批量管理)
- **物品** — 左侧位置树(显示子节点物品数);搜索;CSV 导入/导出
- **位置** — Finder 风格文件夹浏览,双击进入,hover 出现操作按钮
- **记录** — 按物品/动作/位置/时间筛选,实时统计本批次合计
- **诊断** — 麦克风不工作?来这里一眼定位

---

## 端口约定

| 服务 | 容器内 | 对外 |
|---|---|---|
| 前端 nginx HTTPS | 443 | `${APP_PORT:-8443}` |
| 后端 FastAPI | 8000 | 仅内网 |
| Whisper (可选) | 9000 | 仅内网 |

API 文档:`https://<host>:8443/docs` (nginx 反代)

---

## API 速查

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/diag` | 完整诊断信息 |
| GET | `/api/logs?since_id=&level=` | 增量拉取后端日志 |
| POST | `/api/logs/client` | 前端日志上报 |
| GET / POST / PATCH / DELETE | `/api/items[/{id}]` | 物品 CRUD |
| GET | `/api/items/export.csv` | 导出 |
| GET | `/api/items/import-template.csv` | 模板 |
| POST | `/api/items/import?mode=upsert\|append\|replace` | CSV 导入 |
| POST/GET | `/api/items/{id}/transactions` | 单品流水 |
| GET | `/api/transactions?q=&action=&location_id=&since=&until=&limit=` | 全局流水筛选 |
| GET / POST / PATCH / DELETE | `/api/locations[/{id}]` | 位置 CRUD |
| POST | `/api/voice/intent` | `{text, context?}` → IntentResult |
| POST | `/api/voice/transcribe` | 音频上传,Whisper 转写 |
| GET / PATCH | `/api/settings` | 运行时配置(API Key 脱敏) |
| POST | `/api/settings/test-llm` | 测试当前 LLM 配置 |

---

## 数据持久化

```
./data/
├── storage.db       # SQLite 数据库
├── config.json      # LLM / 语音运行时配置 (含 API key,自行注意权限)
└── certs/
    ├── server.crt   # 自签证书 (持久化, 不会每次重建)
    └── server.key
```

备份只需要 tar 这一个目录。

---

## iOS / iPad 注意事项

- **必须 HTTPS**:`getUserMedia` 和 `SpeechRecognition` 要求 secure context。这是 8443 端口存在的全部理由。
- **首次需用户手势**:Safari 第一次访问页面时麦克风权限要点击触发(点大麦克风按钮即可)
- **Web Speech API 联网**:Apple 在云端识别中文,需要 iPad 能联外网。需要纯离线 → 用 Whisper:`./start.sh --whisper`,设置页勾"启用 Whisper"
- **iOS Safari 不擅长长 continuous SR**:本项目的设计已经规避了这个问题 —— 持续监听只为唤醒词、识别一旦命中就立即换成单次 SR
- **更专业的离线唤醒**(可选):接入 Picovoice Porcupine 即可,本仓库不默认集成(需个人 access key)

---

## 项目结构

```
repo_git/
├── start.sh                          # 一键启动/停止/日志
├── docker-compose.yml
├── README.md
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py                   # FastAPI 入口 + access log middleware
│       ├── config.py                 # 持久化的运行时配置 (LLM / Voice)
│       ├── database.py
│       ├── models.py                 # SQLAlchemy: Item / Location / Transaction
│       ├── schemas.py                # Pydantic 输入输出模型
│       ├── llm/
│       │   ├── client.py             # OpenAI-compatible HTTP client (tool 调用 + JSON 兜底)
│       │   └── intent.py             # 意图解析 + 置信度阈值 + 执行
│       ├── services/
│       │   ├── inventory.py          # CRUD + 关键词搜索 + CJK 分词器
│       │   ├── summary.py            # 给 LLM 的上下文摘要
│       │   └── logbuffer.py          # 内存环形缓冲日志
│       └── routers/
│           ├── items.py              # CRUD / CSV 导入导出 / 流水筛选
│           ├── locations.py
│           ├── voice.py              # /intent + /transcribe(Whisper 代理)
│           ├── settings.py
│           └── diag.py               # /diag /logs /logs/client
│
└── frontend/
    ├── Dockerfile
    ├── entrypoint.sh                 # 启动时自动生成自签证书
    ├── nginx.conf                    # HTTPS + /api 反代
    ├── vite.config.js
    ├── tailwind.config.js
    ├── postcss.config.js
    ├── package.json
    ├── index.html
    ├── public/manifest.webmanifest
    └── src/
        ├── main.js
        ├── style.css
        ├── App.vue                   # 顶部导航 + tab 路由
        ├── api.js                    # 所有 fetch 封装
        ├── composables/
        │   ├── useVoice.js           # SR 原语: wake / capture / yes-no / speak
        │   ├── useAudioMeter.js      # AudioContext + Analyser → 频谱
        │   └── useClientLog.js       # 全局错误捕获 + 上报后端
        └── components/
            ├── VoicePanel.vue        # 状态机 UI: idle/command/confirm/processing
            ├── Waveform.vue
            ├── ItemList.vue          # 树筛 + 表格 + 备注 + 导入导出
            ├── ItemEditor.vue
            ├── LocationManager.vue   # Finder 风格文件夹浏览器
            ├── LocationTree.vue      # 物品页侧边栏树
            ├── LocationTreeNode.vue  # 递归节点
            ├── TransactionFeed.vue   # 流水筛选 + 统计 + 导出
            ├── SettingsPanel.vue     # LLM 预设 + 语音配置 + 测试连接
            └── LogsPanel.vue         # 诊断 + 自检按钮 + 合并日志查看器
```

---

## 开发模式(无 Docker)

```bash
# 后端
cd backend
pip install -r requirements.txt
DATABASE_URL=sqlite:///./data/storage.db CONFIG_PATH=./data/config.json \
  uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm install
VITE_BACKEND=http://localhost:8000 npm run dev
# 浏览器打开 http://localhost:5173
```

注意:开发模式没有 HTTPS,**麦克风只能在 `http://localhost`** 上工作(浏览器把 localhost 视为安全源)。要在局域网其他设备测语音,还是用 `./start.sh` 起完整 HTTPS 容器。

---

## 故障排查

| 现象 | 检查项 |
|---|---|
| 麦克风按钮无反应 | 进 **诊断** 页看 secure context / mediaDevices,大概率是用了 HTTP |
| 唤醒词不触发 | 检查唤醒词配置非空;Safari 后台运行可能掉麦,前台保持页面打开 |
| LLM 报错 502 | **诊断** 页"测试 LLM";检查 `base_url` + `api_key`;Ollama 注意 `host.docker.internal:11434/v1` |
| 中文识别奇怪 | iOS 联网识别;或勾选 Whisper 走本地 |
| 自签证书警告 | iPad Safari 一次性"高级 → 继续访问"即可,证书有效期 10 年 |

更详细可查看 **诊断** 页的实时日志。

---

## 演进路线 / 改动历史

```
v0.1  最小可用                   docker compose 起 FastAPI + Vue + SQLite
                                 OpenAI 兼容 LLM client (tool / JSON 双模式)
                                 关键词预筛 + 摘要 → LLM → 置信度
                                 浏览器 SpeechRecognition + speechSynthesis

v0.2  iOS 适配 + 体验改进
   ├─ 自签 HTTPS:解决 Safari 麦克风限制
   ├─ 物品页:位置树侧栏 + 备注内联 + CSV 导入导出
   ├─ 语音页:hero 渐变 + 圆形按钮 + 候选项卡
   └─ 流水页:全字段筛选 + 统计 + 导出

v0.3  统一 + 文件夹化 + 可观测
   ├─ 单端口 8443:nginx 反代 /api + /docs,后端 / Whisper 不再对外
   ├─ start.sh:一键启动/停止/日志/--whisper profile
   ├─ Finder 风格位置浏览器:面包屑、卡片网格、hover 操作、循环检测
   ├─ 流水筛选页 (q / action / location / 时间区间 / 导出)
   ├─ 实时频谱波形 (AudioContext + Analyser)
   └─ 诊断 & 日志页:浏览器能力自检 + 5 个测试按钮 + 前后端合并日志

v0.4  完整语音状态机 (当前版本)
   ├─ useVoice 原语化:startWakeListening / captureUtterance / listenYesNo / speak
   ├─ 唤醒监听轻量化:只取 final 片段, buffer ≤80 字, 命中即停, 切换单次 SR
   ├─ 大麦克风按钮 (主操作) + 唤醒监听小按钮 (副开关)
   ├─ 状态机:idle → command → confirm-text → processing → confirm-action → speaking
   ├─ LLM 前确认:省 token,语音/按钮双通道,可在设置关闭
   ├─ 低置信度确认 (默认阈值 0.5):语音/按钮双通道,听不清自动重问
   └─ 唤醒词配置变更实时生效 (ref 而非快照)
```
