# Quick Fix Guide

## Common Errors and Solutions

### Error: "No devices found"

**Solution:** The script now automatically uses Chrome (web). Just run:
```bash
./start_all.sh
```

### Error: "Flutter is not installed"

**Solution:** Install Flutter:
1. Download from: https://flutter.dev/docs/get-started/install
2. Add to PATH
3. Run: `flutter doctor`

### Error: "Cannot connect to server"

**Solution:** 
1. Make sure backend started successfully
2. Check: `curl http://localhost:8000/health`
3. If backend failed, check logs: `tail -20 /tmp/market_analysis_backend.log`

### Error: "Permission denied" on scripts

**Solution:**
```bash
chmod +x start_all.sh
chmod +x backend/start_dev.sh
chmod +x backend/start_server.sh
chmod +x flutter_app/start.sh
```

### Error: Backend starts but frontend fails

**Solution:** Start them separately:
```bash
# Terminal 1
cd backend && ./start_dev.sh

# Terminal 2  
cd flutter_app && ./start.sh
```

## Testing the Fix

Run the combined script:
```bash
cd /Users/aliyadollahi/Projects/market_analysis
./start_all.sh
```

Expected output:
1. ✅ Backend starts in sandbox mode
2. ✅ Frontend starts (on Chrome if no mobile devices)
3. ✅ Both services running

If you see a specific error, share it and I'll help fix it!




