#!/bin/sh
# Applies pending migrations (idempotent) then starts the requested process.
set -e
case "$1" in
  workers)  python -m db.migrate && exec python -m workers.main ;;
  gateway)  python -m db.migrate && exec uvicorn gateway.app:app --host 0.0.0.0 --port "${PORT:-8000}" ;;
  migrate)  exec python -m db.migrate ;;
  *)        exec "$@" ;;
esac
