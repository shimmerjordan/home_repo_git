<!--
  GitHub Release 正文模板。发布流水线会把 __VERSION__ / __IMAGE__ / __REPO__
  替换成实际值。改这个文件就等于改发布页的说明。
-->
# 语音仓储管家 __VERSION__

家庭杂物仓储管理:在语音 / 钉钉 / Telegram / 飞书里问"充电宝在哪",3D 视图把镜头推到那个抽屉。
本地运行,只把意图解析的少量摘要发给你自己配的 LLM。前后端一个容器,一个 compose 文件搞定。

```bash
mkdir -p voice-storage && cd voice-storage
curl -fLO https://github.com/__REPO__/releases/download/__VERSION__/compose.yml
printf 'LAN_IP=%s\n' "$(hostname -I | awk '{print $1}')" > .env
docker compose up -d
```

`:8080` HTTP 日常访问;`:8443` HTTPS 是 iPad/手机用语音必须走的那个(浏览器要 secure context)。

镜像 `__IMAGE__`(linux/amd64 + linux/arm64)。pull 报 `unauthorized`?说明 GHCR 包还是私有的,
仓库主人到 [包设置](https://github.com/__REPO__/pkgs/container/home_repo_git) 改成 Public。

完整部署方式、端口表、旧双容器版迁移、`.env` 变量、故障排查见
[`docs/deployment.md`](https://github.com/__REPO__/blob/__VERSION__/docs/deployment.md)。
