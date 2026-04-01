# Fixing OpenRouter API Key Issue

## Problem
You're seeing this error:
```
HTTP Request: POST https://openrouter.ai/api/v1/chat/completions "HTTP/1.1 401 Unauthorized"
Error code: 401 - {'error': {'message': 'User not found.', 'code': 401}}
```

**This is NOT about your app user** - it's about your OpenRouter API key!

## Root Cause
The OpenRouter API key in your `.env` file is either:
- Missing
- Invalid/expired
- Not properly formatted
- Not loaded by the server

## Solution

### Step 1: Get a Valid OpenRouter API Key

1. Go to: https://openrouter.ai/keys
2. Sign in or create an account
3. Generate a new API key
4. Copy the key (starts with `sk-or-v1-...`)

### Step 2: Update Your .env File

1. Open the `.env` file in the backend directory:
   ```bash
   cd /Users/aliyadollahi/Projects/market_analysis/backend
   nano .env
   # or use your preferred editor
   ```

2. Add or update the line:
   ```
   OPENROUTER_API_KEY=sk-or-v1-your-actual-key-here
   ```

3. Make sure:
   - No quotes around the key
   - No spaces before/after the `=`
   - The key starts with `sk-or-v1-`
   - No trailing whitespace

### Step 3: Restart the Backend Server

**Important:** You MUST restart the server after changing `.env` file!

```bash
# Stop the current server (Ctrl+C in terminal 1)
# Then restart it:
cd /Users/aliyadollahi/Projects/market_analysis/backend
./start_server.sh sandbox
```

### Step 4: Verify It Works

Check the server logs when it starts. You should see:
```
✅ OpenRouter API key loaded successfully
```

Instead of:
```
⚠️  OPENROUTER_API_KEY not found in environment variables
```

## Verify Your Key Format

Your `.env` file should look like this:
```bash
OPENROUTER_API_KEY=sk-or-v1-abc123def456...
USE_SANDBOX=true
```

## Common Issues

### Issue 1: Key not loaded
- **Problem:** Server started before you added the key
- **Solution:** Restart the server after adding the key

### Issue 2: Invalid key format
- **Problem:** Key doesn't start with `sk-or-v1-`
- **Solution:** Make sure you copied the full key from OpenRouter

### Issue 3: Key expired
- **Problem:** Key was revoked or expired
- **Solution:** Generate a new key from https://openrouter.ai/keys

### Issue 4: Wrong file location
- **Problem:** `.env` file is in the wrong directory
- **Solution:** Make sure it's in `/Users/aliyadollahi/Projects/market_analysis/backend/.env`

## Testing

After fixing, try loading a learning module again. The error should be gone and you should see module content generated!

## Need Help?

1. Check server logs for any warnings about the API key
2. Verify your `.env` file is in the correct location
3. Make sure you restarted the server after changing `.env`
4. Test your API key directly:
   ```bash
   curl https://openrouter.ai/api/v1/models \
     -H "Authorization: Bearer YOUR_API_KEY_HERE"
   ```

If all else fails, generate a new API key from OpenRouter.




