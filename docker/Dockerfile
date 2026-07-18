# ============================================================
# Stage 1: Build frontend
# ============================================================
FROM node:24-slim AS frontend-builder

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

ARG FRONTEND_REPO=https://github.com/Coolkids/nas-tools-web.git
RUN git clone --depth=1 ${FRONTEND_REPO} /frontend \
    && cd /frontend \
    && npm install \
    && npm run build

# ============================================================
# Stage 2: Final image
# ============================================================
FROM debian:13-slim

LABEL maintainer="Coolkids"

ARG TARGETPLATFORM
ARG REPO_URL=https://github.com/Coolkids/nas-tools.git

# 系统运行时依赖 + 编译工具（部分包在 Python 3.14 下可能无预编译 wheel）
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libxml2-dev \
    libxslt-dev \
    libffi-dev \
    libjpeg-dev \
    git \
    tzdata \
    curl \
    fuse3 \
    inotify-tools \
    ffmpeg \
    nginx-full \
    unzip \
    7zip \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv（Rust 编写的 Python 包管理器，加速构建）
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}" \
    UV_PYTHON_INSTALL_DIR="/opt/python" \
    UV_LINK_MODE=copy

# 安装 Python 3.14（uv 下载预编译二进制，无需从源码编译）
RUN uv python install 3.14 \
    && ln -sf "$(uv python find 3.14)" /usr/local/bin/python3 \
    && ln -sf "$(uv python find 3.14)" /usr/local/bin/python

# 升级 pip 工具链
RUN uv pip install --system --upgrade pip setuptools wheel Cython \
    && rm -rf /tmp/* /root/.cache

# 安装 rclone、mc
RUN curl -fsSL https://rclone.org/install.sh | bash \
    && if [ "$TARGETPLATFORM" = "linux/amd64" ]; then ARCH=amd64; elif [ "$TARGETPLATFORM" = "linux/arm64" ]; then ARCH=arm64; else ARCH=amd64; fi \
    && curl -fsSL https://dl.min.io/client/mc/release/linux-${ARCH}/mc --create-dirs -o /usr/bin/mc \
    && chmod +x /usr/bin/mc

# 复制前端构建产物
COPY --from=frontend-builder /frontend/dist /app/web/dist

# 创建运行用户
RUN groupadd -r nastool && useradd -r -g nastool -d /app -s /usr/sbin/nologin nastool

# nginx 配置
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
RUN sed -i 's/^user .*/user nastool;/' /etc/nginx/nginx.conf

# supervisor 配置
RUN mkdir -p /var/run/supervisor
COPY docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 克隆后端代码并安装依赖（使用 uv 加速）
RUN git clone -b main ${REPO_URL} /app/nas-tools --depth=1 --recurse-submodule \
    && uv pip install --system -r /app/nas-tools/requirements.txt \
    && rm -rf /tmp/* /root/.cache

# 环境变量
ENV LANG="C.UTF-8" \
    TZ="Asia/Shanghai" \
    NASTOOL_CONFIG="/config/config.yaml" \
    PS1="\u@\h:\w \$ " \
    UMASK=000 \
    WORKDIR="/app/nas-tools"

WORKDIR ${WORKDIR}

# 配置 Python 路径
RUN SITE_PACKAGES=$(python3 -c "import site; print(site.getsitepackages()[0])") \
    && echo '/app/nas-tools' > "$SITE_PACKAGES/nas-tools.pth" \
    && echo 'fs.inotify.max_user_watches=524288' >> /etc/sysctl.conf \
    && echo 'fs.inotify.max_user_instances=524288' >> /etc/sysctl.conf \
    && chmod +x /app/nas-tools/docker/entrypoint.sh

# 设置目录权限
RUN chown -R nastool:nastool /app /var/run/supervisor /var/lib/nginx /var/log/nginx

USER nastool

EXPOSE 3010
VOLUME ["/config"]
ENTRYPOINT ["/app/nas-tools/docker/entrypoint.sh"]
