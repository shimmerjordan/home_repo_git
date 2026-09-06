#!/usr/bin/env bash
# 从 docker-compose.yml 渲染出发布用的两份 compose。
#
#   scripts/render-compose.sh <镜像地址> cli   → 命令行版, 保留 ${VAR} + .env 用法
#   scripts/render-compose.sh <镜像地址> nas   → NAS 版, 一个变量都不含
#
# 为什么要两份:
#   NAS 的图形界面 (QNAP Container Station / 群晖 Container Manager) 的 YAML 输入框
#   **不做变量插值**, 粘一份带 ${HTTP_PORT:-8080} 的进去会直接以 invalid reference
#   format 失败。所以 NAS 那份必须把变量全部展开成字面量。
#
# 为什么从 docker-compose.yml 生成而不是各存一份:
#   以前维护过一份独立的 deploy/compose.release.yml, 结果它和源文件慢慢就不一样了。
#   只留一个源头, 发布时现渲染, 没有第二份文件可以跑偏。
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${1:?用法: render-compose.sh <镜像地址> <cli|nas>}"
MODE="${2:?用法: render-compose.sh <镜像地址> <cli|nas>}"

# build: . → 钉死版本的 image。
# pull_policy: missing = 本地有就用本地, 没有才拉 —— 升级时不会因为 latest 变了
# 就悄悄换掉正在跑的版本, 换版本是显式动作。
render_base() {
  sed "s|build: \.|image: ${IMAGE}\n    pull_policy: missing|" docker-compose.yml
}

case "$MODE" in
  cli)
    render_base
    ;;
  nas)
    cat <<'HEADER'
# ─────────────────────────────────────────────────────────────────────────
# NAS 版: 变量已全部展开成字面量, 可以整段粘进 QNAP Container Station /
# 群晖 Container Manager 的「创建应用程序」YAML 框 (那些界面不做变量插值)。
#
# 粘进去之前改三个地方:
#   1. LAN_HOSTS  —— 填 NAS 的 mDNS 名字 (如 nas.local)。不填的话 iPad 上
#      HTTPS 不受信任, 而浏览器只在安全上下文里给麦克风权限, 语音会用不了。
#      填 IP 也行, 但 IP 是 DHCP 发的会变, 主机名不会。
#   2. 卷路径 —— 默认写的是 /share/Container/...。**别放到 /share/Web 下**,
#      那是 NAS Web 服务器的根目录, 会把 config.json (含 LLM api_key) 和
#      certs/ca.key (本地 CA 私钥) 暴露到 HTTP 上。
#   3. 端口 —— **只映射 HTTPS 8443 这一个**。项目默认还有个 HTTP 口 (8080),
#      但它在 QNAP 上必然撞 QTS 管理界面, 而且单靠 HTTPS 也够用: 证书就在设置页
#      里下, /ca.crt 走 HTTPS 一样能下 (content-type 已经是 iOS 认的那个)。
#      少开一个口, 也就少一次端口冲突。
#      8443 若在你 NAS 上被占了, 改成别的再粘, 先核一眼:
#        netstat -tln | awk '{print $4}' | grep -oE '[0-9]+$' | sort -un
#
# 第一次访问会弹"不安全" —— 本地 CA 还没装到设备上, 这是预期的。点"高级/显示
# 详细信息 → 继续访问", 进设置页 → 局域网语音访问 → 下载 CA 证书, 装好并信任
# 之后就不再弹了。iOS 还要去 设置 → 通用 → 关于本机 → 证书信任设置 打开完全信任。
#
# 这份**不含 whisper 服务**。语音默认走浏览器自带的识别 (whisper_enabled 默认关),
# 不装它一样能用。去掉的原因是 Container Station 不认 compose 的 profiles ——
# 即使 whisper 挂着 profiles: ["whisper"] 没被激活, 它照样会去 Docker Hub 拉,
# 而 registry-1.docker.io 在国内常年超时, 整个应用会因为这个可选服务创建失败。
# 真要用本地 Whisper: 自己加回那个 service, 并给 Docker Hub 配镜像加速器。
# ─────────────────────────────────────────────────────────────────────────
HEADER
    # 依次: 展开 ${VAR:-default} → 删掉只有 start.sh 用的 LAN_IP 那行和它的说明 →
    # 换端口/卷路径 → 修掉注释里跟着变了的数字 (注释和值对不上比没注释更误导)。
    render_base \
      | sed -E 's/\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}/\1/g' \
      | sed -e '/本机开发时 start.sh 会自动探测/d' \
            -e '/^      - LAN_IP=$/d' \
            -e '/# HTTP 入口: 本机访问 \/ 下载 CA 证书/d' \
            -e '/^      - "8080:80"$/d' \
            -e 's|\./data:/app/data|/share/Container/home_repo/data:/app/data|' \
            -e '/^  whisper:/,/^    profiles: \["whisper"\]$/d' \
            -e 's|# certs 里的本地 CA 换了 iPad 就得重装, 所以别动这个路径。|# 换了这个路径等于换了一套 CA, iPad 上要重装证书 —— 定下来就别再改。|'
    ;;
  *)
    echo "未知模式: $MODE (只支持 cli / nas)" >&2
    exit 2
    ;;
esac
