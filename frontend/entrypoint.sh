#!/bin/sh
set -e

CERT_DIR=/etc/nginx/certs
mkdir -p "$CERT_DIR"

if [ ! -f "$CERT_DIR/server.crt" ] || [ ! -f "$CERT_DIR/server.key" ]; then
  echo "Generating self-signed certificate..."
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CERT_DIR/server.key" \
    -out "$CERT_DIR/server.crt" \
    -subj "/CN=storage.local" \
    -addext "subjectAltName=DNS:storage.local,DNS:localhost,IP:127.0.0.1,IP:0.0.0.0"
fi

exec nginx -g "daemon off;"
