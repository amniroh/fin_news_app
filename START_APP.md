# How to Start the Market Analysis App

## Quick Start (Recommended)

Start both backend and frontend together with one command:

```bash
cd /Users/aliyadollahi/Projects/market_analysis
./start_all.sh
```

This will:
1. ✅ Start backend in **sandbox mode** (no AWS needed)
2. ✅ Start Flutter frontend
3. ✅ Automatically use web (Chrome) if no mobile devices available

## Manual Start (Separate Terminals)

### Terminal 1: Backend (Sandbox Mode)

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
./start_dev.sh
```

Backend will be available at: `http://localhost:8000`

### Terminal 2: Frontend

```bash
cd /Users/aliyadollahi/Projects/market_analysis/flutter_app
./start.sh
```

Or manually:
```bash
cd /Users/aliyadollahi/Projects/market_analysis/flutter_app
flutter pub get
flutter run -d chrome  # For web
# or
flutter run            # For any available device
```

## Platform Options

### Web (Chrome) - Easiest for Testing

```bash
cd flutter_app
flutter run -d chrome
```

### iOS Simulator (macOS only)

```bash
# Start iOS Simulator first
open -a Simulator

# Then run
cd flutter_app
flutter run -d ios
```

### Android Emulator

```bash
# Start Android Emulator first
# Then run
cd flutter_app
flutter run -d android
```

## Troubleshooting

### "No devices found"

**Solution:** Enable web support:
```bash
cd flutter_app
flutter create . --platforms=web
flutter run -d chrome
```

### "Cannot connect to server"

**Check:**
1. Backend is running: `curl http://localhost:8000/health`
2. Backend URL in `lib/services/api_service.dart` is correct
3. For physical devices, use your computer's IP instead of localhost

### Backend not starting

**Check logs:**
```bash
tail -f /tmp/market_analysis_backend.log
```

Or start backend manually to see errors:
```bash
cd backend
./start_dev.sh
```

### Flutter dependencies issues

```bash
cd flutter_app
flutter clean
flutter pub get
```

## Verify Everything is Working

1. **Backend Health Check:**
   ```bash
   curl http://localhost:8000/health
   ```
   Should return: `{"status": "healthy", "database": "sandbox", ...}`

2. **Frontend:**
   - App should open in browser/emulator
   - Should show splash screen
   - Should navigate to onboarding or home

## Development Workflow

1. **Start everything:**
   ```bash
   ./start_all.sh
   ```

2. **Make changes:**
   - Backend: Restart backend server
   - Frontend: Press `r` in Flutter terminal for hot reload

3. **Stop everything:**
   - Press `Ctrl+C` in the terminal running `start_all.sh`
   - Or stop backend and frontend separately

## Notes

- **Sandbox Mode**: Backend uses in-memory storage (no AWS needed)
- **Data Persistence**: Sandbox data saved to `backend/sandbox_data.json`
- **Hot Reload**: Frontend supports hot reload (press `r`)
- **API Key**: Optional for testing, but needed for LLM features

Happy coding! 🚀

