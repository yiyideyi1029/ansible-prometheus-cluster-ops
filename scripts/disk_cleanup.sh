#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/var/log/app"
RETENTION_DAYS=7

echo "[$(date '+%F %T')] 开始清理 ${LOG_DIR} 下超过 ${RETENTION_DAYS} 天的历史归档日志..."
if [ -d "${LOG_DIR}" ]; then
    find "${LOG_DIR}" -type f \( -name "*.log.*" -o -name "*.gz" \) -mtime +${RETENTION_DAYS} -delete
fi
echo "[$(date '+%F %T')] 历史日志清理完成。"