#!/bin/bash
# 享客虾 Worker 守护脚本 — 杀僵尸进程
# cron: */5 * * * * /home/ubuntu/xiaolongxia/scripts/watchdog_workers.sh

MAX_AGE_SEC=180  # 超过 3 分钟视为僵尸

# 找所有 hermes_worker.py 进程
pids=$(pgrep -f 'hermes_worker\.py')
if [ -z "$pids" ]; then
    exit 0
fi

now=$(date +%s)
killed=0

for pid in $pids; do
    # 获取进程启动时间（elapsed seconds）
    elapsed=$(ps -o etimes= -p $pid 2>/dev/null | tr -d ' ')
    if [ -n "$elapsed" ] && [ "$elapsed" -gt "$MAX_AGE_SEC" ]; then
        echo "[$(date)] KILL zombie worker PID=$pid (age=${elapsed}s)" >> /tmp/xkx_watchdog.log
        kill -9 $pid 2>/dev/null
        killed=$((killed + 1))
    fi
done

if [ "$killed" -gt 0 ]; then
    echo "[$(date)] Killed $killed zombie worker(s)" >> /tmp/xkx_watchdog.log
fi
