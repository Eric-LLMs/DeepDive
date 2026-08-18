#!/usr/bin/env bash
# Launch the DeepDive desktop workbench (Electron), bringing up the backend
# first if it is not already running.
#
# Flow:
#   1. Probe the FastAPI backend at http://localhost:8300/health.
#   2. If it is down, start the infra (postgres/redis via docker compose) when
#      those ports are not listening, then start uvicorn in the background.
#   3. Launch the Electron window (unsetting ELECTRON_RUN_AS_NODE — see main.js).
#
# If the backend cannot be brought up (e.g. Docker daemon is off), the workbench
# still opens in offline mode: the file tree, viewer, and screenshots need no
# backend; chat / media generation do.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BACKEND_URL="http://localhost:8300"
BACKEND_HEALTH="$BACKEND_URL/health"
PG_PORT=15432
REDIS_PORT=16379
PYTHON=".venv/Scripts/python.exe"
DESKTOP_DIR="apps/desktop"
LOG_DIR="data"
UVICORN_LOG="$LOG_DIR/uvicorn.log"
PID_FILE="$LOG_DIR/uvicorn.pid"

# True when the FastAPI backend answers /health.
backend_up() {
  curl -fsS --max-time 2 "$BACKEND_HEALTH" >/dev/null 2>&1
}

# True when something is listening on localhost:$1. Uses netstat (LISTENING state)
# rather than curl: on Windows, a curl to a closed port times out (exit 28) instead
# of returning connection-refused (exit 7), so curl cannot tell "closed" from
# "listening but speaks a non-HTTP protocol" (postgres / redis).
port_open() {
  netstat -ano 2>/dev/null | grep -Eq "TCP\s+\S*:$1\s+\S+\s+LISTENING"
}

start_infra() {
  echo "    Starting infrastructure (postgres, redis) ..."
  if docker compose up -d postgres redis; then
    echo "    Waiting for postgres:$PG_PORT / redis:$REDIS_PORT ..."
    for _ in $(seq 1 30); do
      if port_open "$PG_PORT" && port_open "$REDIS_PORT"; then
        echo "    Infrastructure ready."
        return 0
      fi
      sleep 2
    done
    echo "    !!! Timed out waiting for infrastructure." >&2
  else
    echo "    !!! docker compose failed (is Docker Desktop running?)." >&2
    echo "        Backend may not start; launching the workbench in offline mode." >&2
  fi
}

start_backend() {
  echo "    Starting backend on $BACKEND_URL ..."
  mkdir -p "$LOG_DIR"
  "$PYTHON" -m uvicorn apps.api.main:app --port 8300 >>"$UVICORN_LOG" 2>&1 &
  echo $! > "$PID_FILE"
  for _ in $(seq 1 45); do
    if backend_up; then
      echo "    Backend healthy (pid $(cat "$PID_FILE"), log: $UVICORN_LOG)."
      return 0
    fi
    # If uvicorn crashed (e.g. DB unreachable), don't wait out the whole loop.
    local uvicorn_pid
    uvicorn_pid="$(cat "$PID_FILE" 2>/dev/null || echo 0)"
    if ! kill -0 "$uvicorn_pid" 2>/dev/null; then
      echo "    !!! Backend process exited early — see $UVICORN_LOG." >&2
      return 1
    fi
    sleep 2
  done
  echo "    !!! Backend did not become healthy. See $UVICORN_LOG." >&2
}

if backend_up; then
  echo "Backend already running at $BACKEND_URL."
else
  echo "Backend not running at $BACKEND_URL."
  if ! port_open "$PG_PORT" || ! port_open "$REDIS_PORT"; then
    start_infra
  else
    echo "    Infrastructure already listening (postgres:$PG_PORT / redis:$REDIS_PORT)."
  fi
  # Guard against set -e: a non-zero return here (backend failed to boot) must
  # not abort the script before the Electron window is launched.
  start_backend || true
fi

echo "Launching desktop workbench ..."
cd "$DESKTOP_DIR"
unset ELECTRON_RUN_AS_NODE
exec npm start
