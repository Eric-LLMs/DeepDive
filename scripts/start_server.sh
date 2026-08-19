#!/usr/bin/env bash
# One-click launcher for a headless Linux server: backend + infra + React web UI.
# Safe to run every time: each step is skipped when its target is already up.
#
# Progress (a [n/N] banner is printed before every step):
#   [1] Backend already up?             -> skip straight to the web UI
#   [2] Docker Engine installed?        -> auto-install via get.docker.com
#   [3] Docker daemon ready?            -> systemctl enable --now docker, wait
#   [4] All dependency services up      -> postgres/redis/embedding/tts/litellm/worker
#   [5] Python venv + pip deps ensured  -> create .venv, pip install -e ".[dev]"
#   [6] Backend started + admin verified-> uvicorn boot seeds admin/admin
#   [7] React web UI built + served     -> vite preview at :5173 (proxies /api etc.)
#
# Designed for Linux servers — there is no Electron client here; the browser-facing
# entry point is the web UI. Windows desktops use scripts/start_desktop.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BACKEND_URL="http://localhost:8300"
BACKEND_HEALTH="$BACKEND_URL/health"
PG_PORT=15432
REDIS_PORT=16379
WEB_PORT=5173
PYTHON_BIN=".venv/bin/python"
WEB_DIR="apps/web"
LOG_DIR="data"
UVICORN_LOG="$LOG_DIR/uvicorn.log"
PID_FILE="$LOG_DIR/uvicorn.pid"
WEB_LOG="$LOG_DIR/web.log"
COMPOSE_SERVICES="postgres redis embedding tts llm-gateway worker"
DOCKER="docker"

# ── progress helpers ───────────────────────────────────────────────────────────
TOTAL=7
N=0
step() { N=$((N + 1)); printf '\n[%d/%d] %s\n' "$N" "$TOTAL" "$1"; }
ok()   { printf '      [OK] %s\n' "$1"; }
skip() { printf '      [SKIP] %s\n' "$1"; }
warn() { printf '      [!!] %s\n' "$1" >&2; }

# True when the FastAPI backend answers /health.
backend_up() { curl -fsS --max-time 2 "$BACKEND_HEALTH" >/dev/null 2>&1; }

# True when something is listening on localhost:$1 (ss preferred, netstat fallback).
port_open() {
  ss -ltn 2>/dev/null | grep -Eq ":$1\s" || netstat -an 2>/dev/null | grep -Eq ":$1\s.*LISTEN"
}

# True when the Docker daemon answers `docker info`.
docker_ok() { $DOCKER info >/dev/null 2>&1; }

ensure_docker() {
  if docker_ok; then
    ok "Docker found."
    return 0
  fi
  # The current user may not be in the docker group yet — fall back to sudo.
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
    ok "Docker found (via sudo)."
    return 0
  fi
  if command -v docker >/dev/null 2>&1; then
    warn "Docker CLI present but daemon down; will try to start it."
    return 0
  fi
  warn "Docker not found — installing Docker Engine (official script)."
  if command -v curl >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sudo sh >/dev/null 2>&1 || true
  fi
  if command -v docker >/dev/null 2>&1; then
    ok "Docker installed."
    if ! docker_ok && command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
      DOCKER="sudo docker"
    fi
    return 0
  fi
  warn "Could not auto-install Docker Engine. Install it manually, then re-run."
  return 1
}

wait_docker() {
  if docker_ok; then
    ok "Docker daemon ready."
    return 0
  fi
  ( command -v systemctl >/dev/null 2>&1 && sudo systemctl enable --now docker ) >/dev/null 2>&1 \
    || ( command -v service >/dev/null 2>&1 && sudo service docker start ) >/dev/null 2>&1 \
    || true
  printf '      Waiting for the Docker daemon'
  for _ in $(seq 1 30); do
    if docker_ok; then
      echo
      ok "Docker daemon ready."
      return 0
    fi
    printf '.'
    sleep 3
  done
  echo
  warn "Docker daemon did not start — infra may be unavailable."
  return 1
}

start_infra() {
  printf '      Starting: %s ...\n' "$COMPOSE_SERVICES"
  if $DOCKER compose up -d $COMPOSE_SERVICES; then
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
    python3 -m venv .venv || { warn "python3 not found — install Python 3.11+ first."; return 1; }
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
    ok "Admin login OK (admin / admin) — sign in from the web UI."
    return 0
  fi
  warn "admin/admin login check failed — see $UVICORN_LOG."
  return 1
}

start_backend() {
  mkdir -p "$LOG_DIR"
  "$PYTHON_BIN" -m uvicorn apps.api.main:app --host 0.0.0.0 --port 8300 >>"$UVICORN_LOG" 2>&1 &
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
  if ! command -v npm >/dev/null 2>&1; then
    warn "npm not found — skipping the web UI (API is still available)."
    return 1
  fi
  if [ ! -d "$WEB_DIR/node_modules" ]; then
    printf '      Installing web deps (npm install) ...\n'
    ( cd "$WEB_DIR" && npm install ) || true
  fi
  if [ ! -f "$WEB_DIR/dist/index.html" ]; then
    printf '      Building the web frontend (npm run build) ...\n'
    ( cd "$WEB_DIR" && npm run build ) || true
  fi
  if [ ! -f "$WEB_DIR/dist/index.html" ]; then
    warn "Web build failed — check apps/web; API is still available."
    return 1
  fi
  ( cd "$WEB_DIR" && exec npm run preview -- --host >"$REPO_ROOT/$WEB_LOG" 2>&1 & ) || true
  ok "Web UI serving at http://0.0.0.0:$WEB_PORT (log: $WEB_LOG)."
}

# ── main flow ──────────────────────────────────────────────────────────────────
echo "=============================================="
echo "  DeepDive launcher (Linux server)"
echo "=============================================="

step "Checking backend at $BACKEND_URL"
if backend_up; then
  ok "Backend already running."
  N=$((TOTAL - 1))   # steps 2-6 skipped — only the web serving step remains
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

echo
echo "=============================================="
echo "  Ready."
echo "    API / docs : http://localhost:8300/docs"
echo "    Web UI     : http://<this-server-ip>:$WEB_PORT  (admin / admin)"
echo "=============================================="
