#!/bin/bash
# Development startup script - starts server in sandbox mode by default

echo "🚀 Starting Market Analysis Backend in Development Mode"
echo "   Using Sandbox Database (no AWS required)"
echo ""

cd "$(dirname "$0")"
./start_server.sh sandbox

