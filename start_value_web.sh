#!/bin/bash
# Start value-metrics website (FastAPI backend + React frontend)

set -euo pipefail

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/value_web"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
API_BASE="${API_BASE:-http://localhost:${BACKEND_PORT}}"

echo -e "${GREEN}🚀 Starting Value Metrics Web (Backend + Frontend)${NC}"
echo ""

cleanup() {
  echo ""
  echo -e "${YELLOW}🛑 Shutting down...${NC}"
  if [[ -n "${BACKEND_PID:-}" ]]; then
    kill "$BACKEND_PID" 2>/dev/null || true
  fi
  if [[ -n "${FRONTEND_PID:-}" ]]; then
    kill "$FRONTEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ ! -d "$BACKEND_DIR" ]]; then
  echo -e "${YELLOW}❌ backend/ not found${NC}"
  exit 1
fi
if [[ ! -d "$FRONTEND_DIR" ]]; then
  echo -e "${YELLOW}❌ value_web/ not found${NC}"
  exit 1
fi

echo -e "${BLUE}🔵 Starting backend on :${BACKEND_PORT}${NC}"
cd "$PROJECT_DIR"

# Kill anything on backend port if present (best-effort).
if command -v lsof >/dev/null 2>&1; then
  if lsof -ti:"$BACKEND_PORT" >/dev/null 2>&1; then
    echo -e "${YELLOW}Port ${BACKEND_PORT} is in use; killing existing process...${NC}"
    lsof -ti:"$BACKEND_PORT" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
fi

VALUE_METRICS_DB_PATH="${VALUE_METRICS_DB_PATH:-$BACKEND_DIR/data/value_metrics.sqlite}" \
  VALUE_METRICS_CACHE_TTL_SECONDS="${VALUE_METRICS_CACHE_TTL_SECONDS:-1800}" \
  .venv/bin/python "$BACKEND_DIR/main.py" > /tmp/value_web_backend.log 2>&1 &
BACKEND_PID=$!

sleep 2
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
  echo -e "${YELLOW}❌ Backend failed to start. Tail:${NC}"
  tail -50 /tmp/value_web_backend.log || true
  exit 1
fi
echo -e "${GREEN}✅ Backend started (PID: $BACKEND_PID)${NC}"
echo "   API base: ${API_BASE}"
echo ""

echo -e "${BLUE}🟣 Starting frontend on :${FRONTEND_PORT}${NC}"
cd "$FRONTEND_DIR"

if [[ ! -d node_modules ]]; then
  echo "   Installing frontend deps..."
  npm install
fi

export VITE_API_BASE="${API_BASE}"
npm run dev -- --host --port "$FRONTEND_PORT" > /tmp/value_web_frontend.log 2>&1 &
FRONTEND_PID=$!

sleep 2
if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
  echo -e "${YELLOW}❌ Frontend failed to start. Tail:${NC}"
  tail -50 /tmp/value_web_frontend.log || true
  exit 1
fi

echo -e "${GREEN}✅ Frontend started (PID: $FRONTEND_PID)${NC}"
echo "   Frontend: http://localhost:${FRONTEND_PORT}"
echo ""
echo "Logs:"
echo "  Backend : /tmp/value_web_backend.log"
echo "  Frontend: /tmp/value_web_frontend.log"
echo ""
echo -e "${GREEN}Press Ctrl+C to stop both.${NC}"

wait

