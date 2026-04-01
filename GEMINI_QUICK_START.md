# Quick Start: Using Gemini API Instead of OpenRouter

## 🚀 Fast Setup (3 Steps)

### 1. Get Gemini API Key
- Go to: https://makersuite.google.com/app/apikey
- Sign in and click "Create API Key"
- Copy your key

### 2. Update .env File

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
nano .env
```

Add these lines:
```bash
USE_GEMINI=true
GEMINI_API_KEY=your-gemini-key-here
```

(You can comment out or remove `OPENROUTER_API_KEY` if you want)

### 3. Install & Restart

```bash
# Install Gemini library
cd /Users/aliyadollahi/Projects/market_analysis/backend
source venv/bin/activate
pip install google-generativeai

# Restart server
./start_server.sh sandbox
```

Look for this in the logs:
```
✅ Gemini API initialized successfully
```

## ✅ Done!

All LLM features now use Gemini instead of OpenRouter!

**Benefits:**
- ✅ Free tier available
- ✅ No credit card needed (for free tier)
- ✅ Works with all features (chat, learning modules, feed, etc.)

## Check Which Provider Is Active

```bash
curl http://localhost:8000/health | grep llm_provider
```

That's it! 🎉




