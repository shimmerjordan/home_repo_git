# 部署、配置、故障排查

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
APP_PORT=9443 ./start.sh     # 自定义 HTTPS 端口
HTTP_PORT=8090 ./start.sh    # 自定义 HTTP 端口
```

暴露**两个端口**(均可改),不强制 HTTPS。后端、Whisper 都仅在 docker 内网:

- **HTTP `8080`** — 日常访问,无自签证书弹窗。注意:浏览器麦克风语音 (getUserMedia / SpeechRecognition) 要求 secure context,HTTP 下用不了(localhost 除外);文字输入、查看、3D、群机器人都不受影响
- **HTTPS `8443`** — 需要在 iPad / 手机浏览器上用语音时走这个。第一次会有自签证书警告,点 **高级 → 继续访问** 信任即可(证书持久化在 `./data/certs/`,以后不再重生成)

## 端口约定

| 服务 | 容器内 | 对外 |
|---|---|---|
| 前端 nginx HTTP | 80 | `${HTTP_PORT:-8080}` |
| 前端 nginx HTTPS | 443 | `${APP_PORT:-8443}` |
| 后端 FastAPI | 8000 | 仅内网 |
| Whisper (可选) | 9000 | 仅内网 |

API 文档:`http://<host>:8080/docs` 或 `https://<host>:8443/docs` (nginx 反代)

## 首次配置

进入 **设置** 页签:

### LLM 配置
1. 点一个预设(OpenAI / 硅基流动 / DeepSeek / Ollama / 智谱 GLM)或手填
2. 填入 API Key、调整 model
3. 点 **测试连接** — 应该返回一个简短回答证明通了
4. 不支持 tool calling 的模型(如老 Ollama)取消勾选 "支持工具调用"
5. 加速:勾选 **极速模式** + 把 `max_tokens` 调到 256~512

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
./data/
├── storage.db       # SQLite 数据库
├── config.json      # LLM / 语音 / 机器人运行时配置 (含 API key,自行注意权限)
└── certs/
    ├── server.crt   # 自签证书 (持久化, 不会每次重建)
    └── server.key
```

备份只需要 tar 这一个目录。

### 数据兼容性

后端启动时执行两道一次性迁移(均幂等):

1. **`_ensure_columns()`** — SQLite ALTER 补齐 `geometry / uuid / pos_x / pos_z` 列,回填 UUID
2. **`_migrate_to_home()`** — 若无 `kind='home'` 行但存在 root 位置,自动建一个 `我家` 并把所有 root 改 parent。**ID、几何、items 全部不动**,只 UPDATE `parent_id`,外键完全保留。变更写入 AuditLog 可追溯。

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

注意:开发模式没有 HTTPS,**麦克风只能在 `http://localhost`** 上工作(浏览器把 localhost 视为安全源)。要在局域网其他设备测语音,还是用 `./start.sh` 起完整 HTTPS 容器。

## 故障排查

| 现象 | 检查项 |
|---|---|
| 麦克风按钮无反应 | 进 **诊断** 页看 secure context / mediaDevices,大概率是用了 HTTP |
| iPad RMS 波形为 0 | 已修;若仍然出现,看诊断页 AudioContext state 是否 `running` |
| 唤醒词不触发 | 检查唤醒词配置非空;Safari 后台运行可能掉麦,前台保持页面打开 |
| LLM 报错 502 | **诊断** 页"测试 LLM";检查 `base_url` + `api_key`;Ollama 注意 `host.docker.internal:11434/v1` |
| AI 思考很慢 | 设置页:勾选极速模式 + 把 `max_tokens` 调到 256;选轻量模型(glm-4-flash / qwen2.5-7b) |
| 没有结果朗读 | 设置页 "朗读 AI 结果" 默认开;iOS 偶尔需要触屏一次唤醒 speechSynthesis |
| 钉钉机器人没反应 | 看 [`docs/bots/dingtalk.md`](bots/dingtalk.md);最常见是公网不可达 |
| Telegram bot 不工作 | NAS 网络可能连不上 telegram.org,需要代理;看 [`docs/bots/telegram.md`](bots/telegram.md) |
| 飞书 bot 不工作 | 看后端日志 `feishu` 行;`lark-oapi` 是否安装;App ID/Secret/事件订阅 stream 模式是否开启 |
| 自签证书警告 | iPad Safari 一次性"高级 → 继续访问"即可,证书有效期 10 年 |
| 中文识别奇怪 | iOS 联网识别;或勾选 Whisper 走本地 |

更详细可查看 **诊断** 页的实时日志。
