#!/bin/sh

cd ${WORKDIR:-/app/nas-tools}

umask "${UMASK}"

exec supervisord -c /etc/supervisor/conf.d/supervisord.conf
