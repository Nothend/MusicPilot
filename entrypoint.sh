#!/bin/sh
set -e

log_time() { date +"%Y-%m-%d %H:%M:%S %Z"; }

# 单目录持久化：只映射 /app/data（数据 + 日志 [+ 可选旧版 config.yaml]）和 /app/downloads 即可。
# 日志写入 /app/logs（logger.py 写死该路径）；当 /app/logs 不是单独挂载的卷时，把它指向
# /app/data/logs。仍单独映射了 ./logs 的老用户不受影响（挂载点存在，不做任何改动）。
if [ -d /app/data ] && ! mountpoint -q /app/logs 2>/dev/null; then
    mkdir -p /app/data/logs
    if [ -d /app/logs ] && [ ! -L /app/logs ]; then
        rmdir /app/logs 2>/dev/null || true   # 镜像里预建的空目录；非空则保留不动
    fi
    [ -e /app/logs ] || ln -s /app/data/logs /app/logs
fi

# 执行调度：默认每天 20-23 点及 0 点整点各一次
CRON_SCHEDULE="${CRON_SCHEDULE:-0 20-23,0 * * *}"
# 可选：容器启动即先跑一次（默认关闭，避免每次重启都触发一轮下载）
RUN_ON_START="${RUN_ON_START:-false}"

# 是否启用 cron 批处理（读取 config.yaml 的 cookie/uid 同步日期歌单）。默认启用；
# 设为 false 则只用 Web 控制台，不注册 crontab、也不执行 RUN_ON_START。
CRON_ENABLE="${CRON_ENABLE:-true}"

if [ "$CRON_ENABLE" = "true" ]; then
    echo "[$(log_time)] 配置程序执行定时任务：$CRON_SCHEDULE"

    if [ "$RUN_ON_START" = "true" ]; then
        echo "[$(log_time)] RUN_ON_START=true，立即执行一次同步"
        python -u /app/src/main.py || echo "[$(log_time)] 首次执行返回非零，忽略并继续定时任务"
    fi

    # 写入 crontab 并启动 crond：
    # - cd /app 固定工作目录；
    # - 将任务 stdout/stderr 重定向到 PID 1(crond) 的输出，使 docker logs 能看到每轮执行日志。
    echo "$CRON_SCHEDULE cd /app && /usr/local/bin/python -u /app/src/main.py > /proc/1/fd/1 2>/proc/1/fd/2" | crontab -
else
    echo "[$(log_time)] CRON_ENABLE=false，已关闭 cron 批处理（仅使用 Web 控制台）"
    # 清空可能残留的旧 crontab（容器重建时通常已不存在，不存在则忽略）
    crontab -r 2>/dev/null || true
fi

# 可选：Web 控制台（扫码登录 / 每日推荐 / 定时下载）。默认关闭，不影响原有 cron 批处理。
# 放进守护循环：意外退出 5 秒后自动拉起；crond 仍以 exec 方式作为 PID 1 运行。
if [ "${WEB_ENABLE:-false}" = "true" ]; then
    echo "[$(log_time)] WEB_ENABLE=true，启动 Web 控制台（端口 ${WEB_PORT:-8080}）"
    mkdir -p /app/data
    (
        while true; do
            python -u /app/src/web_server.py > /proc/1/fd/1 2>/proc/1/fd/2 || true
            sleep 5
        done
    ) &
fi

echo "[$(log_time)] 启动 crond..."
exec crond -f -l 2
