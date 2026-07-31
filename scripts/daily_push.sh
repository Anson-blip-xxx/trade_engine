#!/usr/bin/env bash
# 每晚 21:00 自动推送一次（cron: 0 21 * * *）
# 失败会重试；无改动则静默退出。
set -u

REPO=/home/ubuntu/.opencode/trade/trading_engine
LOCK=/tmp/trade_daily_push.lock
LOG=/home/ubuntu/.opencode/trade/logs/daily_push.log

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] 已有推送任务在跑，跳过" >>"$LOG"
    exit 0
fi

cd "$REPO" || exit 1
mkdir -p "$(dirname "$LOG")"

git add -A
if git diff --cached --quiet; then
    echo "[$(date '+%F %T')] 无改动，跳过" >>"$LOG"
    exit 0
fi

git commit -m "auto: daily push $(date '+%F %T')" >>"$LOG" 2>&1

for i in 1 2 3; do
    if git push origin main >>"$LOG" 2>&1; then
        echo "[$(date '+%F %T')] 推送成功" >>"$LOG"
        exit 0
    fi
    echo "[$(date '+%F %T')] 推送失败(第${i}次)，10s 后重试" >>"$LOG"
    sleep 10
done

echo "[$(date '+%F %T')] 推送最终失败，请人工检查" >>"$LOG"
exit 1
