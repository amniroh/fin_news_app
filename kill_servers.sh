#!/bin/bash
# Kill all running Market Analysis servers

echo "🛑 Stopping all Market Analysis servers..."

# Kill processes on port 8000 (backend)
if lsof -ti:8000 > /dev/null 2>&1; then
    echo "   Killing processes on port 8000..."
    lsof -ti:8000 | xargs kill -9 2>/dev/null
    sleep 1
fi

# Kill uvicorn processes
if pgrep -f "uvicorn main:app" > /dev/null; then
    echo "   Killing uvicorn processes..."
    pkill -9 -f "uvicorn main:app" 2>/dev/null
    sleep 1
fi

# Kill Flutter processes
if pgrep -f "flutter run" > /dev/null; then
    echo "   Killing Flutter processes..."
    pkill -9 -f "flutter run" 2>/dev/null
    sleep 1
fi

# Verify port is free
if lsof -ti:8000 > /dev/null 2>&1; then
    echo "⚠️  Warning: Port 8000 may still be in use"
    echo "   Run: lsof -i:8000 to see what's using it"
else
    echo "✅ Port 8000 is now free"
fi

echo "✅ Done! All servers stopped."




