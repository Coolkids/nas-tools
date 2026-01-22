#!/bin/sh

cd ${WORKDIR}
echo "以PUID=${PUID}，PGID=${PGID}的身份启动程序..."
mkdir -p /.local
mkdir -p /.pm2
chown -R "${PUID}":"${PGID}" "${WORKDIR}" /config /usr/lib/chromium /.local /.pm2
export PATH=${PATH}:/usr/lib/chromium

umask "${UMASK}"
exec gosu "${PUID}":"${PGID}" "$(which dumb-init)" "$(which pm2-runtime)" start run.py -n NAStool --interpreter python3
