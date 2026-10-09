#!/bin/sh
set -eu

cd "${WORKDIR:-/app/nas-tools}"

umask "${UMASK:-000}"

# 挂载全新 /config 时也能直接启动；文件缓存和 Valkey 数据互不混用。
mkdir -p /config/cache/valkey

cat > /tmp/nastool-valkey.conf <<'CONF'
bind 127.0.0.1
port 6379
protected-mode yes
daemonize no
dir /config/cache/valkey
dbfilename dump.rdb
appendonly yes
appendfsync everysec
save 900 1
save 300 10
save 60 10000
logfile ""
CONF

# 内置 Valkey 默认无密码且仅容器本机可访问；可用环境变量启用密码。
if [ -n "${VALKEY_PASSWORD:-}" ]; then
    case "$VALKEY_PASSWORD" in
        *"
"*) echo 'VALKEY_PASSWORD 不允许包含换行' >&2; exit 1 ;;
    esac
    task_valkey_password=$(printf '%s' "$VALKEY_PASSWORD" | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf 'requirepass "%s"\n' "$task_valkey_password" >> /tmp/nastool-valkey.conf
fi
chmod 600 /tmp/nastool-valkey.conf

exec supervisord -c /etc/supervisor/conf.d/supervisord.conf
