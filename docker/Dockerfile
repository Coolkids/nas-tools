FROM python:3.14-slim

# 安装必要的包
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    python3-dev \
    python3-pip \
    libxml2-dev \
    libxslt-dev \
    tzdata \
    gosu \
    zip \
    curl \
    fuse3 \
    xvfb \
    inotify-tools \
    chromium-driver \
    npm \
    dumb-init \
    ffmpeg

# 设置时区、安装工具和配置
RUN ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo "${TZ}" > /etc/timezone \
    && curl -fsSL https://rclone.org/install.sh | bash \
    && if [ "$(uname -m)" = "x86_64" ]; then ARCH=amd64; elif [ "$(uname -m)" = "aarch64" ]; then ARCH=arm64; fi \
    && curl -fsSL https://dl.min.io/client/mc/release/linux-${ARCH}/mc --create-dirs -o /usr/bin/mc \
    && chmod +x /usr/bin/mc \
    && python -m pip install --upgrade pip setuptools wheel \
    && pip install Cython \
    && python -m pip install -r https://raw.githubusercontent.com/Coolkids/nas-tools/main/requirements.txt \
    && npm install pm2 -g \
    && rm -rf /tmp/* /root/.cache

# 环境变量
ENV LANG="C.UTF-8" \
    TZ="Asia/Shanghai" \
    NASTOOL_CONFIG="/config/config.yaml" \
    PS1="\u@\h:\w \$ " \
    REPO_URL="https://github.com/Coolkids/nas-tools.git" \
    PYPI_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple" \
    PUID=0 \
    PGID=0 \
    UMASK=000 \
    WORKDIR="/nas-tools"

WORKDIR ${WORKDIR}

# 配置Python路径和系统参数
RUN python_ver=$(python3 -V | awk '{print $2}') \
    && echo "${WORKDIR}/" > /usr/local/lib/python${python_ver%.*}/site-packages/nas-tools.pth \
    && echo 'fs.inotify.max_user_watches=524288' >> /etc/sysctl.conf \
    && echo 'fs.inotify.max_user_instances=524288' >> /etc/sysctl.conf \
    && git config --global pull.ff only \
    && git clone -b main ${REPO_URL} ${WORKDIR} --depth=1 --recurse-submodule \
    && git config --global --add safe.directory ${WORKDIR}

EXPOSE 3000
VOLUME ["/config"]
ENTRYPOINT ["/nas-tools/docker/entrypoint.sh"]
