# Sandbox Mode - Testing Without AWS

The Market Analysis app includes a **sandbox mode** that allows you to test the app locally without setting up AWS DynamoDB. This is perfect for development and demos.

## What is Sandbox Mode?

Sandbox mode uses an **in-memory database** that:
- ✅ Requires no AWS setup
- ✅ Works immediately out of the box
- ✅ Persists data to a local JSON file (optional)
- ✅ Has the same API as DynamoDB version
- ✅ Perfect for testing and demos

## Quick Start with Sandbox Mode

### Option 1: Using the Shell Script (Recommended)

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
./start_server.sh sandbox
```

### Option 2: Using Python Script

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
python start_server.py sandbox
```

### Option 3: Manual Start

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend

# Set environment variable
export USE_SANDBOX=true

# Start server
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

## Switching Between Modes

### Start with Sandbox (No AWS):
```bash
./start_server.sh sandbox
# or
python start_server.py sandbox
```

### Start with DynamoDB (Requires AWS Setup):
```bash
./start_server.sh dynamodb
# or
python start_server.py dynamodb
```

## How It Works

When `USE_SANDBOX=true`:
- The app uses `database_service_sandbox.py` instead of `database_service.py`
- All data is stored in memory
- Optionally persists to `sandbox_data.json` file
- No AWS credentials needed
- No DynamoDB table required

## Sandbox Data Persistence

By default, sandbox mode saves data to `sandbox_data.json` in the backend directory. This means:
- Data persists between server restarts
- You can see all users in the JSON file
- Easy to reset: just delete `sandbox_data.json`

To disable persistence (pure in-memory):
```python
# In database_service_sandbox.py, change:
db_service = SandboxDatabaseService(persist_to_file=False)
```

## Checking Server Mode

Check which mode the server is running in:

```bash
curl http://localhost:8000/health
```

Response will show:
```json
{
  "status": "healthy",
  "service": "Market Analysis Backend",
  "version": "1.0.0",
  "database": "sandbox",
  "timestamp": "2024-01-01T00:00:00",
  "sandbox_stats": {
    "total_users": 5,
    "users": ["user_123", "user_456", ...],
    "persist_to_file": true,
    "data_file": "/path/to/sandbox_data.json"
  }
}
```

## Sandbox Data File

The `sandbox_data.json` file contains all user data in a readable format:

```json
{
  "users": {
    "user_123": {
      "user_id": "user_123",
      "phone_number": "+1234567890",
      "investment_goals": ["retirement"],
      "completed_modules": ["what_is_stock"],
      ...
    }
  },
  "last_updated": "2024-01-01T00:00:00"
}
```

## Resetting Sandbox Data

To clear all sandbox data:

```bash
# Delete the data file
rm backend/sandbox_data.json

# Or programmatically via API (if you add an endpoint)
# Or restart server with fresh data
```

## Testing with Sandbox Mode

1. **Start server in sandbox mode:**
   ```bash
   ./start_server.sh sandbox
   ```

2. **Test the API:**
   ```bash
   # Health check
   curl http://localhost:8000/health
   
   # Create a user (via onboarding)
   curl -X POST http://localhost:8000/onboarding \
     -H "Content-Type: application/json" \
     -d '{
       "user_id": "test_user_123",
       "age": 30,
       "investment_goals": ["retirement"],
       "time_horizon": 10,
       "risk_comfort_level": 3,
       "prior_experience": 1
     }'
   ```

3. **Check sandbox data:**
   ```bash
   cat backend/sandbox_data.json
   ```

## Advantages of Sandbox Mode

✅ **No AWS Setup Required** - Start testing immediately  
✅ **No Costs** - No AWS charges  
✅ **Fast Development** - No network latency  
✅ **Easy Debugging** - See all data in JSON file  
✅ **Isolated Testing** - Won't affect production data  
✅ **Offline Development** - Works without internet  

## When to Use Each Mode

### Use Sandbox Mode When:
- 🧪 Testing locally
- 🎯 Running demos
- 🚀 Quick prototyping
- 📚 Learning the codebase
- 🔧 Developing new features

### Use DynamoDB Mode When:
- 🌐 Deploying to production
- 👥 Multiple developers sharing data
- 📊 Need production-like environment
- 🔄 Testing AWS integration
- 💰 Need to test AWS costs

## Environment Variables

You can also set the mode via environment variable:

```bash
# In .env file
USE_SANDBOX=true   # Use sandbox mode
USE_SANDBOX=false  # Use DynamoDB mode
```

Or export before starting:
```bash
export USE_SANDBOX=true
uvicorn main:app --reload
```

## Troubleshooting

### Server won't start in sandbox mode
- Make sure you're in the backend directory
- Check that `database_service_sandbox.py` exists
- Verify Python dependencies are installed

### Data not persisting
- Check that `sandbox_data.json` is writable
- Look for file permissions issues
- Verify `persist_to_file=True` in the service

### Want to switch modes mid-development
- Just restart the server with the different mode
- Sandbox data won't affect DynamoDB
- DynamoDB data won't affect sandbox

## Next Steps

1. Start the server in sandbox mode
2. Test all the features
3. When ready, set up DynamoDB (see `DYNAMODB_SETUP.md`)
4. Switch to DynamoDB mode for production

Happy testing! 🎉

