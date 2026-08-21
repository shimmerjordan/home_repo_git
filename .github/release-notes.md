<!--
  GitHub Release 正文模板。发布流水线会把 __VERSION__ / __IMAGE__ / __REPO__
  替换成实际值。改这个文件就等于改发布页的说明。
-->
# 语音仓储管家 __VERSION__

家庭杂物仓储管理:在语音 / 钉钉 / Telegram / 飞书里问"充电宝在哪",3D 视图把镜头推到那个抽屉。
本地运行,只把意图解析的少量摘要发给你自己配的 LLM。前后端一个容器,一个 compose 文件搞定。

## 部署

```bash
mkdir -p voice-storage && cd voice-storage
curl -fLO https://github.com/__REPO__/releases/download/__VERSION__/compose.yml
printf 'LAN_IP=%s\n' "$(hostname -I | awk '{print $1}')" > .env
docker compose up -d
```

打开 `http://<NAS-IP>:8080` → **设置**页填 LLM 的 `base_url` / `api_key` / `model` → 开始用。

数据全在 `./data`(库 + 配置 + 日志 + 本地 CA),备份就 `sudo tar` 这一个目录(属主是 root)。

## 两个端口

| 地址 | 用途 |
|---|---|
| `:8080` HTTP | 日常访问。电脑上语音也能用 |
| `:8443` HTTPS | **iPad / 手机用语音只能走这个** —— 浏览器要求 secure context |

**iPad 装一次 CA 就零弹窗**:浏览器打开 `http://<NAS-IP>:8080/ca.crt` → 设置 → 通用 →
VPN与设备管理 → 安装 → 关于本机 → 证书信任设置 → 打开完全信任。
CA 只生成一次,IP 变了也不用重装。懒得装就点"继续访问"。

可选离线转写:`docker compose --profile whisper up -d`(镜像 2GB,吃 1~2G 内存),再到设置页勾"启用 Whisper"。

## 升级 / 回滚

改 `compose.yml` 里的 `image` 版本号,然后 `docker compose up -d`。数据和 CA 都不动,库结构自动迁移。

```bash
docker compose pull && docker compose up -d   # 拉远端同名 tag 的最新
```

## 从"前后端两个容器"迁到这一版

老版本跑的是 `storage-frontend` + `storage-backend`。**数据、配置、设置全部复用,不用导出导入** ——
因为它们本来就都在宿主机的 `./data` 里,新容器挂的是同一个目录。

```bash
cd <你原来放 docker-compose.yml 的目录>
sudo tar czf ../data-backup-$(date +%F).tgz data     # 保险
docker compose down --remove-orphans                 # 停掉旧的两个容器
curl -fLO https://github.com/__REPO__/releases/download/__VERSION__/compose.yml
printf 'LAN_IP=%s\n' "$(hostname -I | awk '{print $1}')" > .env
docker compose -f compose.yml up -d
```

要点:

- **`compose.yml` 必须和原来的 `data/` 目录同级**,这样 `./data:/app/data` 才挂到同一份数据上
- 端口(8080/8443)没变;`data/certs` 里的 CA 没变,**iPad 上装过的证书继续有效**
- `config.json` 原样复用。唯一会被自动改的是 `llm.max_tokens`:低于 2048 会被抬到 4096
  (太小会截断输出,多物品操作会整批丢失),启动日志里会打一行说明
- 旧的 `storage-backend` / `storage-frontend` 容器和镜像可以删了:`docker image prune`
- 还想留在源码编译:`git pull && ./start.sh` 就行,同样不丢数据

验证迁移成功:

```bash
docker compose ps                                   # storage-app 应为 healthy
curl -s localhost:8080/api/diag | head -c 200       # 物品/位置/流水条数和迁移前一致
```

## `.env` 变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LAN_IP` | 空 | 局域网 IP,写进证书 SAN。可多个,空格/逗号分隔 |
| `HTTP_PORT` | `8080` | HTTP 端口 |
| `APP_PORT` | `8443` | HTTPS 端口 |
| `TZ` | `Asia/Shanghai` | 时区 |

## 镜像

`__IMAGE__` — `linux/amd64` + `linux/arm64`,内含 nginx + FastAPI(supervisord 管)。

> pull 提示 `unauthorized`?说明 GHCR 包还是私有的,仓库主人到
> [包设置](https://github.com/__REPO__/pkgs/container/home_repo_git) 改成 Public。

## 排查

```bash
docker compose logs -f --tail=200        # nginx + 后端日志都在这
curl -s localhost:8080/api/diag          # 完整自检
```

| 现象 | 处理 |
|---|---|
| iPad 麦克风点不动 | 必须走 `https://<IP>:8443`,不能是 HTTP + 局域网 IP |
| 多物品操作漏条目 | 设置页 `max_tokens` 别低于 2048 |
| 存入新物品被并到别人头上 | 默认已开"落库前逐条确认",在确认卡片里改目标 |
| 飞书跑久了不响应 | `curl -s localhost:8080/api/diag \| jq .feishu`,会自愈,别反复重启 |

## 文档

[部署](https://github.com/__REPO__/blob/__VERSION__/docs/deployment.md) ·
[架构](https://github.com/__REPO__/blob/__VERSION__/docs/architecture.md) ·
[语音与 LLM](https://github.com/__REPO__/blob/__VERSION__/docs/voice.md) ·
[API](https://github.com/__REPO__/blob/__VERSION__/docs/api.md) ·
[备份](https://github.com/__REPO__/blob/__VERSION__/docs/backup.md) ·
[群机器人](https://github.com/__REPO__/blob/__VERSION__/docs/bots/README.md)

许可证 AGPL-3.0-or-later:通过网络提供服务也须公开完整源码。
