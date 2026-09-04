# 部署、配置、故障排查

## 部署

两种方式,数据目录和端口完全一样,随时可以互换。

### A. 用发布的镜像(推荐,不编译)

```bash
mkdir -p voice-storage && cd voice-storage
curl -fLO https://github.com/shimmerjordan/home_repo_git/releases/latest/download/compose.yml
printf 'LAN_IP=%s\n' "$(hostname -I | awk '{print $1}')" > .env
docker compose up -d
```

Release 页那份 `compose.yml` 的 `image` **钉死版本号**(发布流水线用 `sed` 把仓库根目录
[`docker-compose.yml`](../docker-compose.yml) 里的 `build: .` 换成带版本号的 `image:`
生成,见 `.github/workflows/release.yml`),所以下次 `up -d` 不会莫名换版本。升级 = 改
`image` 那一行或重新下新版本的文件,然后 `up -d`。

`image` 那行没变但远端同名 tag 被重推过(或你钉的是 `:latest`)时,`up -d` 不会去拉 ——
`pull_policy: missing` 只在本地没有镜像时才拉。这种情况要显式刷新:
`docker compose pull && docker compose up -d`。

想要离线转写:`docker compose --profile whisper up -d`(镜像约 2GB,`ASR_MODEL=small`
吃 1~2G 内存),再到设置页勾选"启用 Whisper"。

### `.env` 变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LAN_IP` | 空 | 局域网 IP,写进证书 SAN。可多个,空格/逗号分隔 |
| `HTTP_PORT` | `8080` | HTTP 端口 |
| `APP_PORT` | `8443` | HTTPS 端口 |
| `TZ` | `Asia/Shanghai` | 时区 |

### B. 从源码编译

```bash
./start.sh              # 启动 (后台)
./start.sh --whisper    # 带本地 Whisper STT
./start.sh stop|restart|logs|ps
APP_PORT=9443 HTTP_PORT=8090 ./start.sh    # 改端口
```

`LAN_IP` 由 `start.sh` 自动探测。**改了前端也要重建镜像** —— dist 是在镜像里 build 的。

## 从"前后端两个容器"迁过来

老版本跑 `storage-frontend` + `storage-backend`。**数据、配置、设置全部原样复用,不需要导出导入** ——
它们本来就都在宿主机的 `./data` 里,新容器挂的是同一个目录。

```bash
cd <原来放 docker-compose.yml 的目录>
sudo tar czf ../data-backup-$(date +%F).tgz data    # 保险
docker compose down --remove-orphans                # 停掉旧的两个容器
```

然后按上面 A 或 B 起新的:

- **走 A(镜像)**:把 `compose.yml` 放到**和 `data/` 同级**,`up -d`。同级是硬要求,
  `./data:/app/data` 才挂得上同一份数据
- **走 B(源码)**:`git pull && ./start.sh`,`start.sh` 自带 `--remove-orphans`,旧容器会被清掉

会变的只有一件事:`config.json` 里 `llm.max_tokens` 低于 2048 会被自动抬到 4096
(太小会截断 AI 输出,多物品操作整批丢失),启动日志里打一行说明。其余配置一个字不动。

不变的:端口 8080/8443、`data/` 布局、`data/certs` 里的本地 CA ——
**iPad 上装过的证书继续有效**。

验证:

```bash
docker compose ps                              # storage-app 应为 healthy
curl -s localhost:8080/api/diag | head -c 300  # 物品/位置/流水条数应与迁移前一致
```

旧镜像可以清了:`docker image prune`。

## 端口与容器

| 服务 | 容器 | 容器内 | 对外 |
|---|---|---|---|
| nginx HTTP | `storage-app` | 80 | `${HTTP_PORT:-8080}` |
| nginx HTTPS | `storage-app` | 443 | `${APP_PORT:-8443}` |
| 后端 FastAPI | `storage-app` | `127.0.0.1:8000` | 不暴露 |
| Whisper (可选) | `storage-whisper` | 9000 | 仅 docker 内网 |

- **HTTP 8080** — 日常访问,无证书弹窗。浏览器麦克风要求 secure context,所以 HTTP 下
  用不了语音(localhost 除外);文字输入、3D、群机器人都不受影响
- **HTTPS 8443** — iPad / 手机用语音走这个。装一次本地根 CA 就彻底没弹窗:浏览器打开
  `http://<IP>:8080/ca.crt` → 设置 → 通用 → VPN与设备管理 → 安装 → 关于本机 → 证书信任设置
  → 打开完全信任;不装就点"高级 → 继续访问"。CA 只生成一次,服务器证书每次启动按 `LAN_IP`
  重签(同一个 CA 签的,设备端不用重装,换 IP 也不用重装)

nginx 和 uvicorn 由 supervisord 一起带起(`deploy/supervisord.conf`),`./start.sh logs`
两个进程的日志都能看到;单独重启某一个:
`docker exec storage-app supervisorctl -c /etc/supervisor/conf.d/supervisord.conf restart backend`。
Whisper 的**服务名不能改** —— 后端按 docker DNS 名 `http://whisper:9000` 找它。

API 文档:`http://<host>:8080/docs`。

## 发版 (维护者)

```bash
# 写好根目录 CHANGELOG.md, 然后
git tag v0.8.0 && git push origin v0.8.0
```

`.github/workflows/release.yml` 会:跑 `ci.yml` 当闸门 → 构建 amd64+arm64 推 GHCR →
用 `.github/release-notes.md` 当 release 正文 → 附上版本钉死的 `compose.yml`。

**第一次发布后必须手动**把 GHCR 包设成 Public
([包设置](https://github.com/shimmerjordan/home_repo_git/pkgs/container/home_repo_git)
→ Change visibility),否则别人 pull 会 `unauthorized`。

发布页文案改 [`.github/release-notes.md`](../.github/release-notes.md)。

## 首次配置

进入 **设置** 页签:

### LLM 配置
1. 点一个预设(OpenAI / 硅基流动 / DeepSeek / Ollama / 智谱 GLM)或手填
2. 填入 API Key、调整 model
3. 点 **测试连接** — 应该返回一个简短回答证明通了
4. 不支持 tool calling 的模型(如老 Ollama)取消勾选 "支持工具调用"
5. 加速:勾选 **极速模式**。`max_tokens` **别调低于 2048**,太小会截断输出导致多物品操作丢条目

### 语音配置
- 唤醒词,逗号分隔(默认 `小库,小仓,管家`)
- **置信度阈值** 默认 0.5,低于此值的修改性操作会语音确认
- **发送给 AI 前先口头确认识别文本** 默认开启,省 token,30 秒无应答自动确认
- **朗读 AI 结果** 默认开启,关闭进一步加速响应
- 可选启用 Whisper(纯离线 STT,需 `--whisper` 启动)

### 添加初始数据
- 进 **3D** 页 → `+` 新建一个家(默认会让你迁移已有房间到这个家下)
- 在 2D 平面图工具栏的家具下拉里挑房间/家具,在画布上点击放置
- 进 **物品** 页 → 新增几件物品,或下载 CSV 模板批量导入

## 数据持久化

```
./data/                # 整个目录挂到容器 /app/data, 是唯一的挂载点
├── storage.db       # SQLite 数据库
├── config.json      # LLM / 语音 / 机器人运行时配置 (含 API key,自行注意权限)
├── logs/            # 后端日志
└── certs/
    ├── ca.crt       # 本地根 CA (装到 iPad 上的就是它, 存在就绝不重新生成)
    ├── ca.key
    ├── server.crt   # 每次启动按当前 LAN_IP 重签, CA 不变所以设备无需重装
    └── server.key
```

备份只需要 tar 这一个目录(属主是 root,要 `sudo`)。

### 数据兼容性

后端启动时执行 `backend/app/migrations.py` 里的 `run_all(engine)`(WebDAV 备份恢复后也会调
同一个函数,启动与恢复共用同一套逻辑),依次跑三道一次性迁移(均幂等):

1. **`ensure_columns()`** — SQLite ALTER 补齐 `geometry / uuid / pos_x / pos_z` 列,回填 UUID
2. **`ensure_indexes()`** — 给 `transactions` 表补 `item_id` / `location_id` 索引;老库(建表时
   还没有 `index=True`)靠这一步补齐,流水筛选和 pending-returns 接口不再全表扫
3. **`migrate_to_home()`** — 若无 `kind='home'` 行但存在 root 位置,自动建一个 `我家` 并把所有 root 改 parent。**ID、几何、items 全部不动**,只 UPDATE `parent_id`,外键完全保留。变更写入 AuditLog 可追溯。

CSV 导入同样向后兼容:老的 "客厅 / 沙发" 路径会被自动识别为遗留路径,前面补当前家的名字。新导出的 CSV 自带 "我家 / 客厅 / 沙发" 完整路径。

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

`VITE_BACKEND` **必须显式传**:`frontend/vite.config.js` 里的默认代理目标是历史遗留的
`http://localhost:8765`,不设这个变量, 前端请求 `/api/*` 会打到一个没人监听的端口。

注意:开发模式没有 HTTPS,**麦克风只能在 `http://localhost`** 上工作(浏览器把 localhost 视为安全源)。要在局域网其他设备测语音,还是用 `./start.sh` 起完整 HTTPS 容器。

## 故障排查

| 现象 | 检查项 |
|---|---|
| 麦克风按钮无反应 | 进 **诊断** 页看 secure context / mediaDevices,大概率是用了 HTTP |
| iPad RMS 波形为 0 | 已修;若仍然出现,看诊断页 AudioContext state 是否 `running` |
| 唤醒词不触发 | 检查唤醒词配置非空;Safari 后台运行可能掉麦,前台保持页面打开 |
| LLM 报错 502 | **诊断** 页"测试 LLM";检查 `base_url` + `api_key`;Ollama 注意 `host.docker.internal:11434/v1` |
| AI 思考很慢 | 设置页:勾选极速模式 + 把 `max_tokens` 调到 256;选轻量模型(glm-4-flash / qwen2.5-7b) |
| 多物品操作漏条目 | `max_tokens` 别低于 2048,详见 [`docs/voice.md`](voice.md#max_tokens-别调小) |
| 存入新物品被并到别人头上 | 默认已开"落库前逐条确认",在确认卡片里把候选改成"新建" |
| 飞书跑久了不响应 | `curl -s localhost:8080/api/diag \| jq .feishu`;长连接会自愈,别反复重启容器 |
| 没有结果朗读 | 设置页 "朗读 AI 结果" 默认开;iOS 偶尔需要触屏一次唤醒 speechSynthesis |
| 钉钉机器人没反应 | 看 [`docs/bots/dingtalk.md`](bots/dingtalk.md);最常见是公网不可达 |
| Telegram bot 不工作 | NAS 网络可能连不上 telegram.org,需要代理;看 [`docs/bots/telegram.md`](bots/telegram.md) |
| 飞书 bot 不工作 | 看后端日志 `feishu` 行;`lark-oapi` 是否安装;App ID/Secret/事件订阅 stream 模式是否开启 |
| 自签证书警告 | iPad Safari 一次性"高级 → 继续访问"即可,证书有效期 10 年 |
| 中文识别奇怪 | iOS 联网识别;或勾选 Whisper 走本地 |

更详细可查看 **诊断** 页的实时日志。
