> **License: GNU AGPL-3.0-or-later** —
> 任何人可以自由使用、修改、分发本仓库代码,**但只要分发或通过网络提供服务,
> 都必须将完整源码(含修改部分)以同样的 AGPL-3.0 协议公开**。
> 详见根目录 [`LICENSE`](LICENSE)。商业闭源使用请先联系作者获取例外授权。

# 语音仓储管家 (Voice Storage)

[![CI](https://github.com/shimmerjordan/home_repo_git/actions/workflows/ci.yml/badge.svg)](https://github.com/shimmerjordan/home_repo_git/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/shimmerjordan/home_repo_git?sort=semver)](https://github.com/shimmerjordan/home_repo_git/releases/latest)

[部分功能视频示例](https://www.bilibili.com/video/BV1vZ5a69EvH/?share_source=copy_web)

家庭杂物仓储管理系统,通过**语音 / 钉钉群 / Telegram / 飞书群**查找、存放、取出物品。
前端跑在 iPad 浏览器,服务端用 Docker Compose 部署在 N5105 这类工控 NAS 上 —— **前后端同一个容器** (`storage-app`: nginx + FastAPI, supervisord 带起),完全本地运行,只把意图解析的少量摘要文字发给可配置的 OpenAI 兼容 LLM。

支持**多个"家"**(我家 / 老家 / 父母家),3D 立体可视化所有房间和家具,语音找到东西时自动推进相机 + 高亮目标 + 周围淡出。

## 跑起来

```bash
mkdir -p voice-storage && cd voice-storage
curl -fLO https://github.com/shimmerjordan/home_repo_git/releases/latest/download/compose.yml
printf 'LAN_HOSTS=%s\n' "$(hostname).local" > .env   # iPad 走 HTTPS 用语音时要
docker compose up -d
```

打开 `http://<NAS-IP>:8080` → 设置页填 LLM API key → 开干。
iPad 用语音走 `https://<NAS-IP>:8443`,在设置页下载并信任本地 CA 后零弹窗。

NAS 图形界面 (QNAP Container Station / 群晖 Container Manager) 用发布页上那份
**compose.nas.yml**: 不含变量、只开 HTTPS 一个口, 可以整段粘进去。

从源码编译、旧双容器版迁移、端口/环境变量、发版流程都在 [`docs/deployment.md`](docs/deployment.md)。

---

## 文档导航

| 模块 | 说明 |
|---|---|
| **[deployment](docs/deployment.md)** | 两种部署方式 / 端口约定 / 双容器迁移 / 首次配置 / 数据持久化 / 发版流程 / 故障排查 |
| **[architecture](docs/architecture.md)** | 运行时拓扑 / 项目结构 / 数据模型 / 启动顺序 / LLM 摘要算法 |
| **[voice](docs/voice.md)** | 语音状态机 / LLM 配置 / 加速 tips / iOS 注意事项 |
| **[api](docs/api.md)** | REST API 速查 + OpenAPI 文档入口 |
| **[backup](docs/backup.md)** | WebDAV 备份 / 选择性 + GFS 分层保留 / AES 加密 / 恢复 |
| **[bots/](docs/bots/README.md)** | 群机器人接入总览 + 对比表 |
| **[CHANGELOG](CHANGELOG.md)** | 版本演进 |

---

## 功能一览

- **仓储核心**:物品(名称/别名/分类/标签/数量/单价/位置)+ 无限层级位置 + 多个"家" + 流水与审计日志 + CSV 导入导出(老 CSV 向后兼容)
- **多渠道交互**:iPad 语音(唤醒词 + 双层确认 + TTS)、3D 立体可视化、2D 平面图编辑器、钉钉/Telegram/飞书群机器人
- **落库前人工确认**(默认开启):会改数据的操作先出方案候选,不直接写库;名字相近默认建新物品,不悄悄合并;流水页支持 ↩ 回撤
- **WebDAV 备份**:选择性组件 + GFS 日/周/月分层保留 + AES-256 加密,坚果云/Nextcloud/群晖等均可([配置](docs/backup.md))
- **LLM 接入**(完全可配置):OpenAI 兼容 + Anthropic `/v1/messages`,预设 OpenAI/硅基流动/DeepSeek/Ollama/智谱/Claude/cc-trans。
  `max_tokens` 别调太小,详见 [`docs/voice.md`](docs/voice.md)
- **诊断 & 日志**:浏览器能力自检、后端状态卡片、前后端合并日志(3s 刷新,来源/级别/关键字过滤)

完整版本历史见 [`CHANGELOG.md`](CHANGELOG.md)。
