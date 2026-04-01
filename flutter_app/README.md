# Market Analysis Flutter App

## Prerequisites

1. **Flutter SDK** - Install from https://flutter.dev/docs/get-started/install
2. **Backend running** - Make sure the backend server is running (see backend README)

## Quick Start

### 1. Install Dependencies

```bash
cd /Users/aliyadollahi/Projects/market_analysis/flutter_app
flutter pub get
```

### 2. Check Flutter Setup

```bash
flutter doctor
```

Make sure you have:
- ✅ Flutter SDK installed
- ✅ Dart SDK installed
- ✅ A device/emulator available (iOS Simulator, Android Emulator, or physical device)

### 3. Configure Backend URL (Optional)

The app defaults to `http://localhost:8000` in debug mode.

To change the backend URL, edit `lib/services/api_service.dart`:
```dart
const localUrl = 'http://localhost:8000';  // Change this if needed
```

For physical devices, use your computer's IP address:
```dart
const localUrl = 'http://192.168.1.XXX:8000';  // Replace with your IP
```

### 4. Run the App

**For iOS Simulator:**
```bash
flutter run -d ios
```

**For Android Emulator:**
```bash
flutter run -d android
```

**For Web (Chrome):**
```bash
flutter run -d chrome
```

**For any available device:**
```bash
flutter run
```

## Running on Physical Device

### iOS Device

1. Connect your iPhone via USB
2. Trust the computer on your iPhone
3. Run: `flutter run -d ios`

### Android Device

1. Enable Developer Options and USB Debugging on your Android device
2. Connect via USB
3. Run: `flutter run -d android`

### For Physical Devices - Update Backend URL

Since physical devices can't access `localhost`, you need to:

1. Find your computer's IP address:
   ```bash
   # On macOS/Linux:
   ifconfig | grep "inet " | grep -v 127.0.0.1
   
   # On Windows:
   ipconfig
   ```

2. Update `lib/services/api_service.dart`:
   ```dart
   const localUrl = 'http://YOUR_IP_ADDRESS:8000';
   ```

3. Make sure your backend allows connections from your network (check CORS settings)

## Development Commands

```bash
# Run in debug mode (hot reload enabled)
flutter run

# Run in release mode
flutter run --release

# Build APK (Android)
flutter build apk

# Build iOS app
flutter build ios

# Run tests
flutter test

# Analyze code
flutter analyze
```

## Troubleshooting

### "No devices found"
- Make sure you have a simulator/emulator running
- Or connect a physical device
- Check with: `flutter devices`

### "Cannot connect to server"
- Make sure backend is running: `cd backend && ./start_dev.sh`
- Check backend URL in `api_service.dart`
- For physical devices, use your computer's IP instead of localhost

### "Package not found" errors
- Run: `flutter pub get`
- Clean and rebuild: `flutter clean && flutter pub get`

### Build errors
- Run: `flutter clean`
- Then: `flutter pub get`
- Then: `flutter run`

## Hot Reload

While the app is running:
- Press `r` in the terminal to hot reload
- Press `R` to hot restart
- Press `q` to quit

## Project Structure

```
lib/
├── main.dart                 # App entry point
├── screens/                  # All app screens
│   ├── onboarding_welcome_screen.dart
│   ├── onboarding_questions_screen.dart
│   ├── home_screen.dart
│   ├── learning_modules_screen.dart
│   ├── portfolio_simulation_screen.dart
│   ├── chat_screen.dart
│   └── progress_screen.dart
├── services/                 # API and storage services
│   ├── api_service.dart
│   └── user_service.dart
├── models/                   # Data models
│   └── onboarding_data.dart
└── widgets/                  # Reusable widgets
    ├── error_widget.dart
    └── loading_widget.dart
```

## Next Steps

1. ✅ Start backend: `cd ../backend && ./start_dev.sh`
2. ✅ Run Flutter app: `flutter run`
3. ✅ Complete onboarding
4. ✅ Explore all features!

Happy coding! 🚀

