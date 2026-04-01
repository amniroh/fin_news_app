#!/bin/bash
# Quick start script for Flutter app

echo "🚀 Starting Market Analysis Flutter App"
echo ""

# Check if Flutter is installed
if ! command -v flutter &> /dev/null; then
    echo "❌ Flutter is not installed or not in PATH"
    echo "   Install Flutter from: https://flutter.dev/docs/get-started/install"
    exit 1
fi

# Check if we're in the right directory
if [ ! -f "pubspec.yaml" ]; then
    echo "❌ Error: pubspec.yaml not found"
    echo "   Make sure you're in the flutter_app directory"
    exit 1
fi

# Install dependencies
echo "📦 Installing dependencies..."
flutter pub get

# Check for available devices
echo ""
echo "📱 Checking for available devices..."
DEVICES=$(flutter devices --machine | grep -c "device" || echo "0")

if [ "$DEVICES" -eq "0" ]; then
    echo "⚠️  No devices found. Enabling web support..."
    flutter create . --platforms=web 2>/dev/null || true
    echo "🌐 Starting on Chrome (web)..."
    flutter run -d chrome
else
    echo ""
    echo "🎯 Starting app..."
    echo "   Press 'r' for hot reload, 'R' for hot restart, 'q' to quit"
    echo ""
    flutter run
fi

