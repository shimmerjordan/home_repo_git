#!/usr/bin/env bash
# 启动 / 重启所有服务. 用法:
#   ./start.sh              # 启动 (后台)
#   ./start.sh --whisper    # 同时启动本地 Whisper STT 服务
#   ./start.sh --logs       # 启动后跟随日志
#   ./start.sh stop         # 停止
#   ./start.sh restart      # 重启
#   APP_PORT=9443 ./start.sh    # 自定义 HTTPS 端口
#   HTTP_PORT=8090 ./start.sh   # 自定义 HTTP 端口
set -euo pipefail
cd "$(dirname "$0")"

CMD=(up -d --build)
PROFILES=()
FOLLOW=false

case "${1:-}" in
  stop)    docker compose down; exit 0 ;;
  restart) docker compose restart; exit 0 ;;
  logs)    docker compose logs -f --tail=200; exit 0 ;;
  ps)      docker compose ps; exit 0 ;;
esac

for arg in "$@"; do
  case "$arg" in
    --whisper) PROFILES+=(--profile whisper) ;;
    --logs)    FOLLOW=true ;;
  esac
done

mkdir -p data data/certs

# 探测本机所有 IPv4, 写进服务器证书 SAN (让 iPad 用 https://<局域网IP> 时证书匹配)。
# 可手动覆盖: LAN_IP="192.168.1.50 10.0.0.2" ./start.sh
export LAN_IP="${LAN_IP:-$(hostname -I 2>/dev/null || true)}"

if [ ${#PROFILES[@]} -gt 0 ]; then
  docker compose "${PROFILES[@]}" "${CMD[@]}"
else
  docker compose "${CMD[@]}"
fi

PORT="${APP_PORT:-8443}"
HTTP_PORT="${HTTP_PORT:-8080}"
IP=$(echo "$LAN_IP" | awk '{print $1}')
[ -z "$IP" ] && IP="<NAS-IP>"

echo
echo "=========================================="
echo "  ✅ 已启动"
echo
echo "  本机(电脑)访问 —— HTTP 即可, 语音也能用:"
echo "    http://127.0.0.1:$HTTP_PORT"
echo
echo "  iPad / 手机(局域网)用语音 —— 需 HTTPS, 一次性装信任本地 CA 后零弹窗:"
echo "    1) 用设备浏览器打开   http://$IP:$HTTP_PORT/ca.crt   下载证书"
echo "    2) iOS: 设置 → 通用 → VPN与设备管理 → 安装该描述文件"
echo "       再到 设置 → 通用 → 关于本机 → 证书信任设置 → 打开对该 CA 的完全信任"
echo "    3) 之后访问          https://$IP:$PORT              不再弹\"不安全\""
echo
echo "  停止: ./start.sh stop    日志: ./start.sh logs"
echo "=========================================="

if $FOLLOW; then
  docker compose logs -f --tail=100
fi
