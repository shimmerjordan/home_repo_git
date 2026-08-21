# ---------------------------------------------------------------------------
# 单镜像: nginx (80/443) + uvicorn (127.0.0.1:8000), 由 supervisord 一起带起。
# 合并前是 storage-frontend + storage-backend 两个容器; 对外端口、./data 目录布局、
# 本地 CA 都保持不变, 直接换镜像即可, 不需要重装 iPad 上的证书。
#
# COPY 精确到子目录: 一是层缓存 (改源码不重跑 npm install / pip install), 二是不把宿主
# 的 frontend/node_modules (~86M) 和 frontend/dist 带进镜像。
# ---------------------------------------------------------------------------

# 前端产物是纯 JS/CSS, 与 CPU 架构无关 —— 所以固定在 **构建机本机架构** 上编,
# 别让 buildx 为了出 arm64 镜像把 npm install + vite build 也放进 QEMU 里跑
# (那会慢十倍还容易 OOM)。给 ARG 一个默认值是为了让不带 BuildKit 的经典 builder
# 也能解析这一行 (两种 builder 都实测过)。
ARG BUILDPLATFORM=linux/amd64
FROM --platform=${BUILDPLATFORM} node:20-alpine AS webbuild
WORKDIR /app
# 带上 lock 文件用 npm ci: 同一个 tag 重复构建必须得出同一份前端产物,
# npm install 会按 semver 范围重新解析, 发布场景下不可接受。
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/index.html frontend/vite.config.js frontend/tailwind.config.js frontend/postcss.config.js ./
COPY frontend/public ./public
COPY frontend/src ./src
RUN npm run build


FROM python:3.11-slim
WORKDIR /app

# openssl 是 entrypoint 签发本地 CA/服务器证书用的 (原先靠 nginx:alpine 里 apk add)。
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx supervisor curl tzdata openssl \
    && rm -rf /var/lib/apt/lists/*

# 1) debian 的 nginx 自带一个 default_server, 不删会跟我们的 conf.d/default.conf 抢 80/443;
# 2) 访问/错误日志转到容器 stdout/stderr, 否则只写进容器内文件, docker compose logs 看不到。
RUN rm -f /etc/nginx/sites-enabled/default \
    && ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY --from=webbuild /app/dist /usr/share/nginx/html
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/app-routes.conf /etc/nginx/app-routes.conf
COPY deploy/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /app/data

EXPOSE 80 443

# 只探后端: nginx 起不来 supervisord 会自己重拉, 后端挂了才是真的不可用。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["/entrypoint.sh"]
