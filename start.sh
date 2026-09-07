#!/usr/bin/env sh
# Entry point for a single container deployment.
#
# Render supplies PORT. One worker deliberately: the MCP gateway holds a child
# process and a single pipe, and the rate limiter writes to one SQLite file, so
# a second worker would mean a second child and contention on the same database.
set -e

PORT="${PORT:-8000}"

exec uvicorn src.deploy_app:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers 1 \
  --timeout-keep-alive 65
