# zcode2api — Python(FastAPI) + Node + Chromium
# 网关使用 Python；ZCode 官方验证 SDK 在真实 Chromium 中运行。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ZCODE_HOST=0.0.0.0 \
    ZCODE_PORT=3000 \
    ZCODE_DATA_DIR=/data \
    ZCODE_NODE_PATH=node

WORKDIR /app

# ── Node.js + Chromium（均提供 Debian ARM64 包）─────────────────────────────
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends \
       nodejs chromium xvfb x11vnc novnc websockify tini fonts-liberation fonts-noto-cjk \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# ── Python 依赖（独立分层，便于缓存）────────────────────────────────────────
COPY requirements.txt ./
RUN pip install -r requirements.txt

# ── 求解器 Node 依赖（独立分层）─────────────────────────────────────────────
COPY captcha_node/package.json captcha_node/package-lock.json ./captcha_node/
RUN cd captcha_node && npm ci --omit=dev

# ── 应用源码 ────────────────────────────────────────────────────────────────
COPY . .
RUN chmod +x /app/docker-entrypoint.sh

# ── 构建标识（每次提交都变，置于末尾以免破坏上层 apt/pip/npm 缓存）───────────
ARG ZCA_VERSION=dev
ARG ZCA_COMMIT=unknown
ENV ZCA_VERSION=${ZCA_VERSION} \
    ZCA_COMMIT=${ZCA_COMMIT}
LABEL org.opencontainers.image.title="zca" \
      org.opencontainers.image.version="${ZCA_VERSION}" \
      org.opencontainers.image.revision="${ZCA_COMMIT}" \
      org.opencontainers.image.source="https://github.com/hechushitaoyuan/zca"

# 账号 / 设置持久化目录（建议挂载到宿主机卷）
VOLUME ["/data"]
EXPOSE 3000 6080

# 健康检查：不依赖 curl（构建期已 purge），用镜像内 Python 探活 /health。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/health', timeout=3).status==200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]
CMD ["python", "main.py", "serve"]
