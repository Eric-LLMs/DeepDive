#!/usr/bin/env bash
# One-click launcher for the DeepDive desktop workbench (Electron) on Windows.
# Safe to run every time: each step is skipped when its target is already up.
#
# Progress (a [n/N] banner is printed before every step):
#   [1] Backend already up?             -> skip straight to the web/desktop clients
#   [2] Docker installed?               -> auto-install Docker Desktop (winget)
#   [3] Docker daemon ready?            -> start Docker Desktop and wait
#   [4] All dependency services up      -> postgres/redis/embedding/tts/litellm/worker
#   [5] Python venv + pip deps ensured  -> create .venv, pip install -e ".[dev]"
#   [6] Backend started + admin verified-> uvicorn boot seeds admin/admin
#   [7] React web UI served             -> vite dev server at :5173 (proxies /api)
#   [8] Electron client launched
#
# If the backend cannot be brought up (e.g. a fresh Docker install needs a reboot),
# the workbench still opens in offline mode: the file tree, viewer, and screenshots
# need no backend; chat / media generation do.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BACKEND_URL="http://localhost:8300"
BACKEND_HEALTH="$BACKEND_URL/health"
PG_PORT=15432
REDIS_PORT=16379
PYTHON_BIN=".venv/Scripts/python.exe"
DESKTOP_DIR="apps/desktop"
WEB_DIR="apps/web"
LOG_DIR="data"
UVICORN_LOG="$LOG_DIR/uvicorn.log"
WEB_LOG="$LOG_DIR/web.log"
PID_FILE="$LOG_DIR/uvicorn.pid"
WEB_PORT=5173
COMPOSE_SERVICES="postgres redis embedding tts llm-gateway worker"

# Make the Docker CLI resolvable even before the system PATH refreshes after install.
DOCKER_BIN="/c/Program Files/Docker/Docker/resources/bin"
if [ -d "$DOCKER_BIN" ] && ! command -v docker >/dev/null 2>&1; then
  export PATH="$DOCKER_BIN:$PATH"
fi

# ── progress helpers ───────────────────────────────────────────────────────────
TOTAL=8
N=0
step() { N=$((N + 1)); printf '\n[%d/%d] %s\n' "$N" "$TOTAL" "$1"; }
ok()   { printf '      [OK] %s\n' "$1"; }
skip() { printf '      [SKIP] %s\n' "$1"; }
warn() { printf '      [!!] %s\n' "$1" >&2; }

# True when the FastAPI backend answers /health.
backend_up() { curl -fsS --max-time 2 "$BACKEND_HEALTH" >/dev/null 2>&1; }

# True when something is listening on localhost:$1 (netstat LISTENING state).
port_open() {
  netstat -ano 2>/dev/null | grep -Eq "TCP\s+\S*:$1\s+\S+\s+LISTENING"
}

# True when the docker CLI is on PATH.
docker_available() { command -v docker >/dev/null 2>&1; }

ensure_docker() {
  if docker_available; then
    ok "Docker found."
    return 0
  fi
  warn "Docker not found — installing Docker Desktop (first run only, UAC prompt)."
  if command -v winget >/dev/null 2>&1; then
    winget install -e --id Docker.DockerDesktop \
      --accept-package-agreements --accept-source-agreements >/dev/null 2>&1 || true
  fi
  if docker_available; then
    ok "Docker installed."
    return 0
  fi
  warn "Could not auto-install Docker. Install Docker Desktop manually from"
  warn "  https://www.docker.com/products/docker-desktop/ then re-run this script."
  return 1
}

wait_docker() {
  if docker info >/dev/null 2>&1; then
    ok "Docker daemon ready."
    return 0
  fi
  local desktop="/c/Program Files/Docker/Docker/Docker Desktop.exe"
  if [ -f "$desktop" ]; then
    ( "$desktop" & ) >/dev/null 2>&1 || true
  fi
  printf '      Waiting for the Docker daemon'
  for _ in $(seq 1 60); do
    if docker info >/dev/null 2>&1; then
      echo
      ok "Docker daemon ready."
      return 0
    fi
    printf '.'
    sleep 5
  done
  echo
  warn "Docker daemon did not come up (a fresh install usually needs a Windows reboot)."
  warn "Continuing in offline mode; re-run after reboot."
  return 1
}

start_infra() {
  printf '      Starting: %s ...\n' "$COMPOSE_SERVICES"
  if docker compose up -d $COMPOSE_SERVICES; then
    printf '      Waiting for postgres:%s / redis:%s' "$PG_PORT" "$REDIS_PORT"
    for _ in $(seq 1 30); do
      if port_open "$PG_PORT" && port_open "$REDIS_PORT"; then
        echo
        ok "Infrastructure ready."
        return 0
      fi
      printf '.'
      sleep 2
    done
    echo
    warn "Timed out waiting for infrastructure."
  else
    warn "docker compose failed — backend may not start."
  fi
  return 1
}

ensure_python() {
  if [ -x "$PYTHON_BIN" ]; then
    skip "venv already present."
  else
    printf '      Creating .venv and installing Python deps (pip install -e \".[dev]\") ...\n'
    python -m venv .venv || { warn "python not found — install Python 3.11+ first."; return 1; }
  fi
  # Some venvs ship without pip (e.g. created by a Python without bundled ensurepip);
  # bootstrap it so `pip install` below can never be a silent no-op.
  "$PYTHON_BIN" -m ensurepip --upgrade >/dev/null 2>&1 || true
  "$PYTHON_BIN" -m pip install --quiet -e ".[dev]" || warn "pip install had issues (offline?)."
  ok "Python deps ready."
}

verify_admin_login() {
  local body
  body="$(curl -fsS --max-time 5 -X POST "$BACKEND_URL/admin/login" \
      -H 'Content-Type: application/json' \
      -d '{"username":"admin","password":"admin"}' 2>/dev/null || true)"
  if printf '%s' "$body" | grep -q '"access_token"'; then
    ok "Admin login OK (admin / admin) — sign in straight from the client."
    return 0
  fi
  warn "admin/admin login check failed — see $UVICORN_LOG."
  return 1
}

start_backend() {
  mkdir -p "$LOG_DIR"
  "$PYTHON_BIN" -m uvicorn apps.api.main:app --port 8300 >>"$UVICORN_LOG" 2>&1 &
  echo $! > "$PID_FILE"
  printf '      Waiting for the backend to become healthy'
  for _ in $(seq 1 45); do
    if backend_up; then
      echo
      ok "Backend healthy (pid $(cat "$PID_FILE"), log: $UVICORN_LOG)."
      return 0
    fi
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || echo 0)"
    if ! kill -0 "$pid" 2>/dev/null; then
      echo
      warn "Backend process exited early — see $UVICORN_LOG."
      return 1
    fi
    printf '.'
    sleep 2
  done
  echo
  warn "Backend did not become healthy. See $UVICORN_LOG."
  return 1
}

serve_web() {
  # Start the React web UI (Vite dev server) and wait until :5173 actually answers.
  if ! command -v npm >/dev/null 2>&1; then
    warn "npm not found — skipping the web UI (API + desktop client still available)."
    return 1
  fi
  if [ ! -d "$WEB_DIR/node_modules" ]; then
    printf '      Installing web deps (npm install) ...\n'
    ( cd "$WEB_DIR" && npm install ) || warn "npm install failed."
  fi
  if curl -fsS --max-time 3 "http://localhost:$WEB_PORT/" >/dev/null 2>&1; then
    ok "Web UI already serving at http://localhost:$WEB_PORT."
    return 0
  fi
  printf '      Starting Vite dev server ...\n'
  ( cd "$WEB_DIR" && nohup npm run dev >"$REPO_ROOT/$WEB_LOG" 2>&1 & )
  printf '      Waiting for the web UI'
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "http://localhost:$WEB_PORT/" >/dev/null 2>&1; then
      echo
      ok "Web UI serving at http://localhost:$WEB_PORT (log: $WEB_LOG)."
      return 0
    fi
    printf '.'
    sleep 1
  done
  echo
  warn "Web UI did not become reachable. See $WEB_LOG."
  return 1
}

# ── main flow ──────────────────────────────────────────────────────────────────
echo "=============================================="
echo "  DeepDive launcher (Windows desktop)"
echo "=============================================="

step "Checking backend at $BACKEND_URL"
if backend_up; then
  ok "Backend already running."
  N=$((TOTAL - 2))   # steps 2-6 skipped — web + desktop launch remain
else
  step "Checking Docker"
  ensure_docker || true
  step "Waiting for the Docker daemon"
  wait_docker || true
  step "Starting all dependency services ($COMPOSE_SERVICES)"
  start_infra || true
  step "Ensuring Python environment"
  ensure_python || true
  step "Starting backend + verifying admin login"
  start_backend || true
  if backend_up; then
    verify_admin_login || true
  fi
fi

step "Serving the React web UI"
serve_web || true

step "Launching desktop workbench"
if [ ! -d "$DESKTOP_DIR/node_modules" ]; then
  printf '      Installing desktop deps (npm install) ...\n'
  ( cd "$DESKTOP_DIR" && npm install ) || warn "npm install failed."
fi
cd "$DESKTOP_DIR"
unset ELECTRON_RUN_AS_NODE
exec npm start
