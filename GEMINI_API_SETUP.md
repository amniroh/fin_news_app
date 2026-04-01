# Gemini API Setup - Alternative to OpenRouter

The app now supports using **Gemini API** instead of OpenRouter! This gives you a free alternative for LLM features.

## Quick Setup

### Step 1: Get Gemini API Key

1. **Go to Google AI Studio:**
   - Visit: https://makersuite.google.com/app/apikey
   - Sign in with your Google account

2. **Create API Key:**
   - Click "Create API Key"
   - Copy your key (looks like: `AIzaSy...`)

### Step 2: Update Your .env File

Open your `.env` file in the backend directory:

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
nano .env
```

Add or update these lines:

```bash
# Use Gemini API (recommended - has free tier)
USE_GEMINI=true
GEMINI_API_KEY=your-gemini-api-key-here

# Optional: Keep OpenRouter as backup (can be commented out)
# OPENROUTER_API_KEY=sk-or-v1-...
```

### Step 3: Install Dependencies

The new Gemini library needs to be installed:

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
source venv/bin/activate  # Activate virtual environment
pip install google-generativeai
```

Or if starting fresh:

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
pip install -r requirements.txt
```

### Step 4: Restart Backend Server

**Important:** Restart your backend server to load the new configuration.

```bash
# Stop current server (Ctrl+C)
# Then restart:
cd /Users/aliyadollahi/Projects/market_analysis/backend
./start_server.sh sandbox
```

### Step 5: Verify It's Working

Check the server logs when it starts. You should see:

```
✅ Gemini API initialized successfully
   Using model: gemini-1.5-flash
```

## Configuration Options

### Option 1: Use Gemini Only (Recommended)

```bash
USE_GEMINI=true
GEMINI_API_KEY=your-key-here
# OPENROUTER_API_KEY= (leave empty or commented)
```

### Option 2: Use OpenRouter Only

```bash
# USE_GEMINI=false (or leave unset)
OPENROUTER_API_KEY=your-key-here
```

### Option 3: Use Both (Gemini Priority)

```bash
USE_GEMINI=true
GEMINI_API_KEY=your-key-here
OPENROUTER_API_KEY=your-backup-key-here
```

In this case, Gemini will be used first, with OpenRouter as fallback.

## How It Works

The app now has a **unified LLM service** that:

1. **Checks for Gemini first** (if `USE_GEMINI=true` and key is set)
2. **Falls back to OpenRouter** (if Gemini not available)
3. **Falls back to Gemini** (if OpenRouter fails and Gemini key is available)
4. **Works with both APIs** seamlessly - same interface, different providers

## Benefits of Gemini

✅ **Free Tier Available** - Generous free usage  
✅ **No Credit Card Required** - For free tier  
✅ **Fast Responses** - Good performance  
✅ **Simple Setup** - Easy API key generation  

## Features That Work with Gemini

All LLM features work with Gemini:
- ✅ Learning modules content generation
- ✅ Investment feed items
- ✅ Chat Q&A
- ✅ Investment suggestions
- ✅ Market updates

## Troubleshooting

### Gemini Not Loading

1. **Check API key format:**
   - Should start with `AIzaSy`
   - No spaces or quotes

2. **Check .env file:**
   ```bash
   grep GEMINI .env
   ```

3. **Verify key is valid:**
   - Go to https://makersuite.google.com/app/apikey
   - Check if key is active

### Still Using OpenRouter

If you see "OpenRouter API initialized" instead of Gemini:

1. Check `USE_GEMINI=true` is set in .env
2. Make sure `GEMINI_API_KEY` is set
3. Restart the server

### Check Current Provider

Check which provider is active:

```bash
curl http://localhost:8000/health
```

Look for the `llm_provider` field in the response.

## Switching Between Providers

To switch providers:

1. Update `.env` file
2. **Restart backend server**
3. Check logs for which provider loaded

The app automatically detects and uses the best available option!

## Summary

✅ Added Gemini API support  
✅ Unified LLM service (works with both APIs)  
✅ Easy configuration via environment variables  
✅ Automatic fallback between providers  
✅ All features work with Gemini  

Now you can skip OpenRouter and use Gemini's free tier! 🎉




