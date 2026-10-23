#!/usr/bin/env sh
# Восстановление из копии: ./scripts/restore.sh backups/studsbor_YYYYMMDD_HHMMSS.sql.gz
# ВНИМАНИЕ: текущие данные базы будут заменены.
set -eu

[ $# -eq 1 ] || { echo "Использование: $0 <файл.sql.gz>"; exit 1; }
docker compose stop bot api
docker compose exec -T db psql -U studsbor -d postgres -c "DROP DATABASE IF EXISTS studsbor WITH (FORCE);"
docker compose exec -T db psql -U studsbor -d postgres -c "CREATE DATABASE studsbor OWNER studsbor;"
gunzip -c "$1" | docker compose exec -T db psql -U studsbor -d studsbor
docker compose start bot api
echo "Восстановлено из $1"
