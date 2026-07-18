# ============================================================
# Stage 1: Build Python 3.14 from source
# ============================================================
FROM debian:13-slim AS python-builder

RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    zlib1g-dev \
    libncurses5-dev \
    libncursesw5-dev \
    libreadline-dev \
    libsqlite3-dev \
    libgdbm-dev \
    libdb5.3-dev \
    libbz2-dev \
    libexpat1-dev \
    liblzma-dev \
    tk-dev \
    libffi-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

RUN wget -q https://www.python.org/ftp/python/3.14.0/Python-3.14.0.tgz \
    && tar xzf Python-3.14.0.tgz \
    && cd Python-3.14.0 \
    && ./configure --enable-optimizations --prefix=/usr/local \
    && make -j$(nproc) \
    && make install \
    && cd / && rm -rf Python-3.14.0 Python-3.14.0.tgz

# ============================================================
# Stage 2: Build frontend
# ============================================================
FROM node:22-slim AS frontend-builder

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

ARG FRONTEND_REPO=https://github.com/Coolkids/nas-tools-web.git
RUN git clone --depth=1 ${FRONTEND_REPO} /frontend \
    && cd /frontend \
    && npm install \
    && npm run build

# ============================================================
# Stage 3: Final image
# ============================================================
FROM debian:13-slim

ARG TARGETPLATFORM
ARG REPO_URL=https://github.com/Coolkids/nas-tools.git

# 系统运行时依赖
RUN apt-get update && apt-get install -y \
    git \
    tzdata \
    gosu \
    curl \
    fuse3 \
    inotify-tools \
    dumb-init \
    ffmpeg \
    nginx \
    && rm -rf /var/lib/apt/lists/*

# 复制 Python 3.14
COPY --from=python-builder /usr/local /usr/local

# 安装 rclone、mc
RUN curl -fsSL https://rclone.org/install.sh | bash \
    && if [ "$TARGETPLATFORM" = "linux/amd64" ]; then ARCH=amd64; elif [ "$TARGETPLATFORM" = "linux/arm64" ]; then ARCH=arm64; else ARCH=amd64; fi \
    && curl -fsSL https://dl.min.io/client/mc/release/linux-${ARCH}/mc --create-dirs -o /usr/bin/mc \
    && chmod +x /usr/bin/mc

# pip 基础升级
RUN pip3 install --upgrade pip setuptools wheel Cython \
    && rm -rf /tmp/* /root/.cache

# 复制前端构建产物
COPY --from=frontend-builder /frontend/dist /app/web/dist

# nginx 配置
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
RUN sed -i 's/^user .*/user root;/' /etc/nginx/nginx.conf

# 克隆后端代码并安装依赖
RUN git clone -b main ${REPO_URL} /app --depth=1 --recurse-submodule \
    && pip3 install -r /app/requirements.txt \
    && rm -rf /tmp/* /root/.cache

# 环境变量
ENV LANG="C.UTF-8" \
    TZ="Asia/Shanghai" \
    NASTOOL_CONFIG="/config/config.yaml" \
    PS1="\u@\h:\w \$ " \
    PUID=0 \
    PGID=0 \
    UMASK=000 \
    WORKDIR="/app"

WORKDIR ${WORKDIR}

# 配置 Python 路径
RUN echo '/app/' > /usr/local/lib/python3.14/site-packages/nas-tools.pth \
    && echo 'fs.inotify.max_user_watches=524288' >> /etc/sysctl.conf \
    && echo 'fs.inotify.max_user_instances=524288' >> /etc/sysctl.conf \
    && chmod +x /app/docker/entrypoint.sh

EXPOSE 3010
VOLUME ["/config"]
ENTRYPOINT ["/app/docker/entrypoint.sh"]
