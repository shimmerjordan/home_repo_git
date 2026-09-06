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
#   3. 端口 —— HTTP 用了 8081 而不是默认的 8080: QNAP QTS 管理界面就占着 8080。
#      按你 NAS 上的实际占用挑一个没人用的。
# ─────────────────────────────────────────────────────────────────────────
HEADER
    # 依次: 展开 ${VAR:-default} → 删掉只有 start.sh 用的 LAN_IP 那行和它的说明 →
    # 换端口/卷路径 → 修掉注释里跟着变了的数字 (注释和值对不上比没注释更误导)。
    render_base \
      | sed -E 's/\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}/\1/g' \
      | sed -e '/本机开发时 start.sh 会自动探测/d' \
            -e '/^      - LAN_IP=$/d' \
            -e 's|"8080:80"|"8081:80"|' \
            -e 's|(http://<ip>:8080/ca.crt)|(http://<NAS>:8081/ca.crt)|' \
            -e 's|\./data:/app/data|/share/Container/home_repo/data:/app/data|' \
            -e 's|# certs 里的本地 CA 换了 iPad 就得重装, 所以别动这个路径。|# 换了这个路径等于换了一套 CA, iPad 上要重装证书 —— 定下来就别再改。|'
    ;;
  *)
    echo "未知模式: $MODE (只支持 cli / nas)" >&2
    exit 2
    ;;
esac
