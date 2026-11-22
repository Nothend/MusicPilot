#!/bin/sh
set -e

log_time() { date +"%Y-%m-%d %H:%M:%S %Z"; }

# 单目录持久化：只映射 /app/data（数据库 + 日志）和 /app/downloads 即可。
# 日志写入 /app/logs（logger.py 写死该路径）；当 /app/logs 不是单独挂载的卷时，把它指向
# /app/data/logs。仍单独映射了 ./logs 的老用户不受影响（挂载点存在，不做任何改动）。
if [ -d /app/data ] && ! mountpoint -q /app/logs 2>/dev/null; then
    mkdir -p /app/data/logs
    if [ -d /app/logs ] && [ ! -L /app/logs ]; then
        rmdir /app/logs 2>/dev/null || true   # 镜像里预建的空目录；非空则保留不动
    fi
    [ -e /app/logs ] || ln -s /app/data/logs /app/logs
fi

mkdir -p /app/data
echo "[$(log_time)] 启动 MusicPilot（端口 ${WEB_PORT:-8080}）"
# exec：让 Web 服务成为 PID 1，docker stop 的 SIGTERM 能直接送达；意外退出由 compose 的 restart 策略拉起
exec python -u /app/src/web_server.py
