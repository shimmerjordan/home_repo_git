> **License: GNU AGPL-3.0-or-later** —
> 任何人可以自由使用、修改、分发本仓库代码,**但只要分发或通过网络提供服务,
> 都必须将完整源码(含修改部分)以同样的 AGPL-3.0 协议公开**。
> 详见根目录 [`LICENSE`](LICENSE)。商业闭源使用请先联系作者获取例外授权。

# 语音仓储管家 (Voice Storage)

[部分功能视频示例](https://b23.tv/sc7kjpG)

家庭杂物仓储管理系统,通过**语音 / 钉钉群 / Telegram / 飞书群**查找、存放、取出物品。
前端跑在 iPad 浏览器,后端用 Docker Compose 部署在 N5105 这类工控 NAS 上,完全本地运行,只把意图解析的少量摘要文字发给可配置的 OpenAI 兼容 LLM。

支持**多个"家"**(我家 / 老家 / 父母家),3D 立体可视化所有房间和家具,语音找到东西时自动推进相机 + 高亮目标 + 周围淡出。

```bash
git clone <repo>
cd repo_git
./start.sh
# 浏览器打开 https://<NAS-IP>:8443  → 设置页配置 LLM API key → 开干
```

---

## 文档导航

| 模块 | 说明 |
|---|---|
| **[deployment](docs/deployment.md)** | 快速启动 / 端口约定 / 首次配置 / 数据持久化 / 故障排查 |
| **[architecture](docs/architecture.md)** | 运行时拓扑 / 项目结构 / 数据模型 / 启动顺序 / LLM 摘要算法 |
| **[voice](docs/voice.md)** | 语音状态机 / LLM 配置 / 加速 tips / iOS 注意事项 |
| **[api](docs/api.md)** | REST API 速查 + OpenAPI 文档入口 |
| **[backup](docs/backup.md)** | WebDAV 备份 / 选择性 + GFS 分层保留 / AES 加密 / 恢复 |
| **[wechat-miniprogram](docs/wechat-miniprogram.md)** | 微信小程序可行性调研 + 纯本地架构 + 进度 |
| **[deployment-miniprogram](docs/deployment-miniprogram.md)** | 小程序本地运行 + 个人开发者上架 + 与 NAS 版数据互通 |
| **[miniprogram/](miniprogram/README.md)** | 微信小程序代码 (独立第三端, 与前后端隔离) |
| **[bots/](docs/bots/README.md)** | 群机器人接入总览 + 对比表 |
| ├─ [钉钉](docs/bots/dingtalk.md) | inbound webhook + 加签 (需公网) |
| ├─ [Telegram](docs/bots/telegram.md) | 长轮询 (无公网, 国内需翻墙) |
| └─ [飞书](docs/bots/feishu.md) | Stream Mode WebSocket (**国内 + 无公网,推荐**) |
| **[changelog](docs/changelog.md)** | 版本演进 |

---

## 功能一览

### 仓储核心
- **物品**:名称 / 别名 / 分类 / 标签 / 数量 / 单价 / 备注 / 位置
- **位置**:无限层级文件夹 — 家 → 房间 → 容器 → 抽屉/层 → ...
- **多个家**:顶层 "家" 分组 ("我家" / "老家" / "父母家"),3D 页可切换
- **流水 + 审计日志**:每次取出/存入/盘点都记;每个字段的修改 git-blame 风格可查
- **CSV 导入导出**:路径 `家/房间/箱子/上层` 自动按层级建缺失节点;**老 CSV (无家前缀) 向后兼容**
- **WebDAV 备份**:全量数据备份到坚果云/Nextcloud/群晖等;选择性组件 + GFS 日/周/月分层保留 + 定时 + AES-256 加密 + 一键恢复 ([配置](docs/backup.md))

### 多渠道交互
- **iPad 语音**:大圆按钮 + 唤醒词 + 双层确认 + 30s 沉默自动确认 + TTS 朗读
- **3D 立体可视化**:Three.js 渲染所有房间和家具(床/沙发/椅/桌/冰箱/马桶/电视/盆栽... 都不是单立方体),物品在容器里的实际位置高亮
- **2D 平面图编辑器**:SVG 拖拽家具,锁定模式,多边形房间,自动贴边吸附
- **钉钉群 @机器人** ([配置](docs/bots/dingtalk.md))
- **Telegram bot** ([配置](docs/bots/telegram.md))
- **飞书群机器人** ([配置](docs/bots/feishu.md))

### LLM 接入 (完全可配置)
- OpenAI 兼容协议,任何 `/v1/chat/completions` 的服务都行
- 内置预设:**OpenAI / 硅基流动 / DeepSeek / Ollama / 智谱 GLM**
- 极速模式 + 可调 `max_tokens` 优化中文响应延迟
- 详见 [`docs/voice.md`](docs/voice.md)

### 诊断 & 日志
- 浏览器能力自检(secure context / mediaDevices / SpeechRecognition / AudioContext)
- 后端状态卡片 + 5 个自检按钮
- 前后端日志合并视图,3s 刷新,支持来源 + 级别 + 关键字过滤

---

## 演进

最近: 多家分组 + 钉钉 + Telegram + **飞书 Stream Mode** + 漂亮 3D 家具 mesh + URL 路由 + 30s 沉默自动确认 + iPad RMS 修复。
完整历史见 [`docs/changelog.md`](docs/changelog.md)。
