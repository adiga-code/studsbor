#!/usr/bin/env sh
# Резервная копия базы: ./scripts/backup.sh  (запускать из корня проекта, например по cron).
# Хранит копии за последние KEEP_DAYS дней в каталоге backups/.
set -eu

KEEP_DAYS="${KEEP_DAYS:-30}"
BACKUP_DIR="$(dirname "$0")/../backups"
mkdir -p "$BACKUP_DIR"

FILE="$BACKUP_DIR/studsbor_$(date +%Y%m%d_%H%M%S).sql.gz"
docker compose exec -T db pg_dump -U studsbor -d studsbor --no-owner | gzip > "$FILE"
find "$BACKUP_DIR" -name 'studsbor_*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "Резервная копия: $FILE"
