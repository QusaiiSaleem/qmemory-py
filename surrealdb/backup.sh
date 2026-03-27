#!/bin/bash
# Backup Qmemory SurrealDB — run from local machine against Railway public URL
#
# Usage:
#   ./surrealdb/backup.sh
#
# Required env vars (set in .env or export manually):
#   QMEMORY_SURREAL_URL   — e.g. https://surrealdb-xxx.up.railway.app
#   QMEMORY_SURREAL_PASS  — root password
#
# Output: backup_qmemory_main_YYYY-MM-DD-HHMM.surql.gz

set -euo pipefail

NS="qmemory"
DB="main"
DATE=$(date +%Y-%m-%d-%H%M)
FILE="backup_${NS}_${DB}_${DATE}.surql"

# Convert ws/wss URLs to http/https for CLI
URL="${QMEMORY_SURREAL_URL:?Set QMEMORY_SURREAL_URL}"
URL="${URL/wss:\/\//https://}"
URL="${URL/ws:\/\//http://}"
URL="${URL%/rpc}"

echo "Backing up ${NS}/${DB} from ${URL}..."

surreal export \
    -e "$URL" \
    -u "${QMEMORY_SURREAL_USER:-root}" \
    -p "${QMEMORY_SURREAL_PASS:?Set QMEMORY_SURREAL_PASS}" \
    --namespace "$NS" \
    --database "$DB" \
    "$FILE"

gzip "$FILE"
echo "Backup complete: ${FILE}.gz ($(du -h "${FILE}.gz" | cut -f1))"
