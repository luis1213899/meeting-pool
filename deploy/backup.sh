#!/usr/bin/env bash
# 会议号池 SQLite 备份脚本
# 用法：bash backup.sh [DB_PATH] [BACKUP_DIR] [KEEP_COUNT]
# 默认值从环境变量或脚本同级 .env 读取，也可以直接传参。
#
# 建议配合 systemd timer 每日触发：
#   OnCalendar=*-*-* 03:10:00
#
# 对账备份文件名：pool.db.bak-YYYYMMDD-HHMMSS

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# 读取 .env（不覆盖已有环境变量）
if [[ -f "$ROOT_DIR/.env" ]]; then
    while IFS= read -r line; do
        line="${line%%#*}"          # 去掉注释
        line="${line//[$'\r']}"     # 去掉 Windows CR
        [[ -z "${line// }" ]] && continue
        [[ "$line" != *=* ]] && continue
        key="${line%%=*}"
        value="${line#*=}"
        value="${value#\"}" value="${value%\"}"
        value="${value#\'}" value="${value%\'}"
        [[ -z "${!key+x}" ]] && export "$key=$value"
    done < "$ROOT_DIR/.env"
fi

DB_PATH="${1:-${POOL_DB_PATH:-$ROOT_DIR/data/pool.db}}"
BACKUP_DIR="${2:-$(dirname "$DB_PATH")}"
KEEP="${3:-7}"

if [[ ! -f "$DB_PATH" ]]; then
    echo "[backup] 数据库文件不存在：$DB_PATH" >&2
    exit 1
fi

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_DIR/pool.db.bak-$TIMESTAMP"

# 用 SQLite .backup 命令热备份（不影响正在运行的服务）
sqlite3 "$DB_PATH" ".backup '$DEST'"

SIZE="$(du -h "$DEST" | cut -f1)"
echo "[backup] 已备份到 $DEST（$SIZE）"

# 保留最近 KEEP 份，删除更早的
mapfile -t OLD < <(
    ls -t "$BACKUP_DIR"/pool.db.bak-* 2>/dev/null | tail -n "+$((KEEP + 1))"
)
for f in "${OLD[@]}"; do
    rm -f "$f"
    echo "[backup] 已删除旧备份：$f"
done

echo "[backup] 完成，当前保留 $(ls "$BACKUP_DIR"/pool.db.bak-* 2>/dev/null | wc -l) 份"
