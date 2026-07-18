#!/bin/sh

cd ${WORKDIR:-/app}

echo "以 PUID=${PUID}，PGID=${PGID} 的身份启动程序..."
mkdir -p /.local
mkdir -p /.pm2
chown -R "${PUID}":"${PGID}" "${WORKDIR}" /config /.local /.pm2 /var/lib/nginx /var/log/nginx

umask "${UMASK}"

echo "启动 nginx..."
nginx -g "daemon off;" &
NGINX_PID=$!

echo "启动 Flask 后端..."
exec gosu "${PUID}":"${PGID}" "$(which dumb-init)" "$(which pm2-runtime)" start run.py -n NAStool --interpreter python3
