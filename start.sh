#!/usr/bin/env bash
# 启动 / 重启所有服务. 用法:
#   ./start.sh              # 启动 (后台)
#   ./start.sh --whisper    # 同时启动本地 Whisper STT 服务
#   ./start.sh --logs       # 启动后跟随日志
#   ./start.sh stop         # 停止
#   ./start.sh restart      # 重启
#   APP_PORT=9443 ./start.sh   # 自定义端口
set -euo pipefail
cd "$(dirname "$0")"

CMD="up -d --build"
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

docker compose "${PROFILES[@]}" $CMD

PORT="${APP_PORT:-8443}"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$IP" ] && IP="<NAS-IP>"

echo
echo "=========================================="
echo "  ✅ 已启动"
echo "  访问: https://$IP:$PORT"
echo "  iPad 第一次访问点 '高级 → 继续访问' 信任自签证书"
echo "  停止: ./start.sh stop"
echo "  日志: ./start.sh logs"
echo "=========================================="

if $FOLLOW; then
  docker compose logs -f --tail=100
fi
