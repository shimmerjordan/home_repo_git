<!--
  GitHub Release 正文模板。发布流水线会把 __VERSION__ / __IMAGE__ / __REPO__
  替换成实际值, 并在末尾自动追加两份完整的 compose (由 scripts/render-compose.sh
  从仓库根的 docker-compose.yml 现渲染, 所以不会和源文件跑偏)。
-->
# 语音仓储管家 __VERSION__

家庭杂物仓储管理: 在语音 / 钉钉 / Telegram / 飞书里问"充电宝在哪", 3D 视图把镜头推到那个抽屉。
本地运行, 只把意图解析的少量摘要发给你自己配的 LLM。前后端一个容器, 一个 compose 文件搞定。

## 装

**命令行**

```bash
mkdir -p voice-storage && cd voice-storage
curl -fLO https://github.com/__REPO__/releases/download/__VERSION__/compose.yml
printf 'LAN_HOSTS=%s\n' "$(hostname)" > .env      # 或填 IP; 见下方说明
docker compose up -d
```

**NAS 图形界面** (QNAP Container Station / 群晖 Container Manager)

用下面那份 **compose (NAS 图形界面直接粘)**, 整段粘进「创建应用程序」的 YAML 框。
那些界面**不做变量插值**, 所以不能用上面那份带 `${...}` 的。

## 端口

| | 用途 |
|---|---|
| `:8080` (NAS 版是 `:48080`) | HTTP 日常访问 + 下载 CA 证书 |
| `:8443` | HTTPS。**iPad/手机用语音必须走这个** (浏览器只在安全上下文里给麦克风权限) |

NAS 版只改了 HTTP: 8080 在 QNAP 上必然撞 QTS 管理界面。48080 只是"高位端口不
容易撞"这一类里的一个, 不是查证过的安全值 —— 粘之前在 NAS 上自己核一遍:
`netstat -tln | awk '{print $4}' | grep -oE '[0-9]+$' | sort -un`

## `LAN_HOSTS`: 填什么决定 iPad 上语音能不能用

容器每次启动会用一个持久化的本地 CA 给自己签服务器证书, `LAN_HOSTS` 里的地址会写进证书 SAN。
装一次 CA (`http://<地址>:8080/ca.crt`) 之后, `https://<地址>:8443` 就不再弹"不安全"。

**优先填主机名**, 比如 `nas.local` —— 局域网 IP 多半是 DHCP 发的, 变一次证书就得重签;
mDNS 名字跟着设备走, 填一次永久有效。IPv4 也收, 多个用空格或逗号分隔。

留空的话证书里只有 `localhost` / `127.0.0.1`, 局域网设备访问会一直不受信任, 语音用不了。
填了认不出的东西 (例如没替换的 `192.168.x.x`) 会在容器日志里明确报出来。

## 数据

一个卷装全部持久化数据: `storage.db` / `config.json` / `logs` / `certs`。

**别把它放在 NAS 的 Web 共享目录下** (QNAP 是 `/share/Web`)。那里的文件对 HTTP 是公开的,
而 `config.json` 里有你的 LLM api_key, `certs/ca.key` 是本地 CA 的私钥。

## 镜像

`__IMAGE__` (linux/amd64 + linux/arm64)。

`pull` 报 `unauthorized` 说明 GHCR 包还是私有的, 仓库主人到
[包设置](https://github.com/__REPO__/pkgs/container/home_repo_git) 改成 Public,
或在 NAS 上 `docker login ghcr.io`。

`:latest` 只在发布的版本号是**当前最大**时才移动 —— 给旧版本发 hotfix 不会把
按 `latest` 部署的人静默降级。想钉死版本就把 image 那行换成 `__IMAGE__`。

完整部署方式、`.env` 变量表、旧双容器版迁移、故障排查见
[`docs/deployment.md`](https://github.com/__REPO__/blob/__VERSION__/docs/deployment.md)。
