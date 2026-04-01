#!/bin/bash
# Start both backend (sandbox mode) and frontend together

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 Starting Market Analysis App (Backend + Frontend)${NC}"
echo ""

# Kill any existing servers first
echo -e "${YELLOW}🛑 Checking for existing servers...${NC}"
if lsof -ti:8000 > /dev/null 2>&1; then
    echo "   Port 8000 is in use. Killing existing processes..."
    lsof -ti:8000 | xargs kill -9 2>/dev/null
    sleep 2
fi
pkill -f "uvicorn main:app" 2>/dev/null
pkill -f "flutter run" 2>/dev/null
sleep 1
echo ""

# Get the project directory
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
FRONTEND_DIR="$PROJECT_DIR/flutter_app"

# Check if directories exist
if [ ! -d "$BACKEND_DIR" ]; then
    echo -e "${YELLOW}❌ Backend directory not found${NC}"
    exit 1
fi

if [ ! -d "$FRONTEND_DIR" ]; then
    echo -e "${YELLOW}❌ Frontend directory not found${NC}"
    exit 1
fi

# Function to cleanup on exit
cleanup() {
    echo ""
    echo -e "${YELLOW}🛑 Shutting down...${NC}"
    if [ ! -z "$BACKEND_PID" ]; then
        kill $BACKEND_PID 2>/dev/null
        # Also kill any uvicorn processes
        pkill -f "uvicorn main:app" 2>/dev/null
    fi
    if [ ! -z "$FRONTEND_PID" ]; then
        kill $FRONTEND_PID 2>/dev/null
    fi
    exit
}

trap cleanup SIGINT SIGTERM

# Start backend in sandbox mode
echo -e "${BLUE}🔵 Starting Backend (Sandbox Mode)...${NC}"
cd "$BACKEND_DIR"

# Check if start_dev.sh exists and is executable
if [ ! -f "start_dev.sh" ]; then
    echo -e "${YELLOW}❌ start_dev.sh not found in backend directory${NC}"
    exit 1
fi

chmod +x start_dev.sh 2>/dev/null
chmod +x start_server.sh 2>/dev/null

./start_dev.sh > /tmp/market_analysis_backend.log 2>&1 &
BACKEND_PID=$!

# Wait a bit for backend to start
echo "   Waiting for backend to start..."
sleep 3

# Check if backend is running
if ! kill -0 $BACKEND_PID 2>/dev/null; then
    echo -e "${YELLOW}❌ Backend failed to start. Check logs:${NC}"
    tail -20 /tmp/market_analysis_backend.log
    exit 1
fi

echo -e "${GREEN}✅ Backend started (PID: $BACKEND_PID)${NC}"
echo "   Backend running at: http://localhost:8000"
echo ""

# Start frontend
echo -e "${BLUE}📱 Starting Frontend...${NC}"
cd "$FRONTEND_DIR"

# Check if Flutter is installed
if ! command -v flutter &> /dev/null; then
    echo -e "${YELLOW}❌ Flutter is not installed or not in PATH${NC}"
    echo "   Install Flutter from: https://flutter.dev/docs/get-started/install"
    echo "   Backend is still running. Stop it with: kill $BACKEND_PID"
    exit 1
fi

# Enable web support if needed
if [ ! -d "web" ]; then
    echo "   Enabling web support..."
    flutter create . --platforms=web 2>/dev/null || true
fi

# Install dependencies
echo "   Installing dependencies..."
if ! flutter pub get > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  Warning: Some dependencies may have failed to install${NC}"
    echo "   Trying to continue anyway..."
fi

# Start Flutter app
echo -e "${GREEN}✅ Starting Flutter app...${NC}"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Backend:  http://localhost:8000 (Sandbox Mode)"
echo "  Frontend: Starting..."
echo ""
echo "  Press Ctrl+C to stop both services"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Try to run on available device, fallback to web
# Note: flutter run is interactive, so we run it in foreground
# The cleanup trap will handle stopping the backend when Flutter exits
if flutter devices | grep -q "device"; then
    flutter run
else
    echo "   No mobile devices found, starting on Chrome (web)..."
    flutter run -d chrome
fi

# When Flutter exits, cleanup will be called automatically via trap

