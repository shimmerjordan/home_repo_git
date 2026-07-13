#!/bin/sh
set -e

CERT_DIR=/etc/nginx/certs
WEBROOT=/usr/share/nginx/html
mkdir -p "$CERT_DIR"

# ---------------------------------------------------------------------------
# 本地 CA + 服务器证书 (自用零弹窗方案)
#
# 思路: 生成一个持久化的本地根 CA, 用它签发服务器证书。把 CA 证书 (ca.crt) 装到
# iPad/手机上并信任一次, 之后 https://<局域网IP>:8443 就不再弹"不安全"。
# 服务器证书每次启动按当前 LAN_IP 重新签发, CA 保持不变 —— 所以 IP 变了也不用在
# 设备上重装, 已装的 CA 依旧有效。
#
# 满足 Apple iOS/iPadOS 对被信任 TLS 证书的硬性要求:
#   - 使用 SAN (不看 CN)          - EKU = serverAuth
#   - RSA >= 2048, 摘要 SHA-256   - 叶子证书有效期 <= 398 天 (这里取 397)
# ---------------------------------------------------------------------------

CA_KEY="$CERT_DIR/ca.key"
CA_CRT="$CERT_DIR/ca.crt"
SRV_KEY="$CERT_DIR/server.key"
SRV_CRT="$CERT_DIR/server.crt"

if [ ! -f "$CA_KEY" ] || [ ! -f "$CA_CRT" ]; then
  echo "[certs] 生成本地根 CA (仅一次, 持久化在 ./data/certs/) ..."
  openssl genrsa -out "$CA_KEY" 2048
  openssl req -x509 -new -nodes -key "$CA_KEY" -sha256 -days 3650 \
    -subj "/CN=Voice Storage Local CA/O=Voice Storage" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$CA_CRT"
fi

# 组装 SAN: 固定项 + 传入的 LAN_IP (空格/逗号分隔, 可多个)。
ALT="DNS.1=storage.local
DNS.2=localhost
IP.1=127.0.0.1"
n=1
for ip in $(echo "${LAN_IP:-}" | tr ',' ' '); do
  # 只收形如 x.x.x.x 的 IPv4
  case "$ip" in
    *[!0-9.]*) continue ;;
    *.*.*.*)   ALT="$ALT
IP.$((n+1))=$ip"; n=$((n+1)) ;;
  esac
done

echo "[certs] 按当前地址签发服务器证书, SAN:"
echo "$ALT" | sed 's/^/         /'

SAN_CNF=$(mktemp)
cat > "$SAN_CNF" <<EOF
[req]
distinguished_name = dn
[dn]
[v3]
basicConstraints = CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt
[alt]
$ALT
EOF

[ -f "$SRV_KEY" ] || openssl genrsa -out "$SRV_KEY" 2048
openssl req -new -key "$SRV_KEY" -subj "/CN=Voice Storage" -out "$CERT_DIR/server.csr"
openssl x509 -req -in "$CERT_DIR/server.csr" \
  -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
  -days 397 -sha256 -extfile "$SAN_CNF" -extensions v3 \
  -out "$SRV_CRT"
rm -f "$SAN_CNF" "$CERT_DIR/server.csr"

# 把 CA 证书放到 web 根目录, 供设备下载安装: http://<ip>:8080/ca.crt
cp "$CA_CRT" "$WEBROOT/ca.crt"

exec nginx -g "daemon off;"
