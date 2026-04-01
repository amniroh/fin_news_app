# Quick Start Guide

## 🔑 First: Set Up OpenRouter API Key

The app needs an OpenRouter API key for LLM features. Quick setup:

1. Get API key: https://openrouter.ai/keys
2. Create `.env` file in `backend/` directory:
   ```bash
   cd backend
   echo "OPENROUTER_API_KEY=sk-or-v1-your-key-here" > .env
   ```
3. Replace `your-key-here` with your actual API key

**See [OPENROUTER_SETUP.md](OPENROUTER_SETUP.md) for detailed instructions.**

## 🚀 Start Testing Immediately (Sandbox Mode)

No AWS setup required! Start testing the app right away:

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
./start_server.sh sandbox
```

The server will start at: http://localhost:8000

## 📋 Available Startup Commands

### Sandbox Mode (Testing - No AWS)
```bash
./start_server.sh sandbox
# or
python start_server.py sandbox
# or (shortcut)
./start_dev.sh
```

### DynamoDB Mode (Production - Requires AWS)
```bash
./start_server.sh dynamodb
# or
python start_server.py dynamodb
```

## ✅ Verify It's Working

```bash
# Check health endpoint
curl http://localhost:8000/health

# Should return:
# {
#   "status": "healthy",
#   "database": "sandbox",
#   ...
# }
```

## 🧪 Test the API

```bash
# Test onboarding
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

## 📁 Sandbox Data

In sandbox mode, all data is saved to:
```
backend/sandbox_data.json
```

You can view/edit this file to see all users and their data.

## 🔄 Switching Modes

Just restart the server with a different mode:
- Stop server (Ctrl+C)
- Start with new mode: `./start_server.sh [sandbox|dynamodb]`

## 📚 More Information

- **Sandbox Mode Details**: See [SANDBOX_MODE.md](SANDBOX_MODE.md)
- **DynamoDB Setup**: See [DYNAMODB_SETUP.md](DYNAMODB_SETUP.md)
- **Full Documentation**: See [README.md](README.md)

## 🎯 Next Steps

1. ✅ Start server in sandbox mode
2. ✅ Test all features
3. ✅ When ready, set up DynamoDB
4. ✅ Switch to DynamoDB mode for production

Happy coding! 🎉

