# Fixing OpenRouter API Key 401 Unauthorized Error

## The Problem
You're getting this error:
```
POST https://openrouter.ai/api/v1/chat/completions "HTTP/1.1 401 Unauthorized"
```

**This means your API key is invalid, expired, or has no credits.**

## Quick Fix Steps

### Step 1: Get a New API Key

1. **Go to OpenRouter:**
   - Visit: https://openrouter.ai/keys
   - Sign in (or create an account if needed)

2. **Generate a New Key:**
   - Click "Create Key" or "New Key"
   - Copy the entire key (it starts with `sk-or-v1-`)

3. **Check Your Account Credits:**
   - Make sure you have credits in your OpenRouter account
   - Free tier might have limited credits

### Step 2: Update Your .env File

1. **Open the .env file:**
   ```bash
   cd /Users/aliyadollahi/Projects/market_analysis/backend
   nano .env
   # or use your preferred editor
   ```

2. **Update the key line:**
   ```
   OPENROUTER_API_KEY=sk-or-v1-your-new-key-here
   ```

   **Important:**
   - No quotes around the key
   - No spaces before or after the `=`
   - No trailing spaces at the end
   - The entire key should be on one line

3. **Example (correct format):**
   ```
   OPENROUTER_API_KEY=sk-or-v1-abc123def456ghi789jkl012mno345pqr678stu901vwx234yz
   ```

4. **Save the file**

### Step 3: Restart Your Backend Server

**This is CRITICAL - the server must be restarted to load the new key!**

1. **Stop the current server:**
   - Go to your backend terminal
   - Press `Ctrl+C` to stop

2. **Restart the server:**
   ```bash
   cd /Users/aliyadollahi/Projects/market_analysis/backend
   ./start_server.sh sandbox
   ```

3. **Check the logs:**
   - You should see: `✅ OpenRouter API key loaded successfully`
   - If you see warnings, the key still isn't loading correctly

### Step 4: Verify It's Working

1. **Check server logs when starting:**
   ```
   ✅ OpenRouter API key loaded successfully
   ```

2. **Try using a feature:**
   - Load a learning module
   - Or try the chat feature
   - Should work without 401 errors

## Common Issues & Solutions

### Issue 1: Key Still Shows 401 After Update

**Possible causes:**
- Server wasn't restarted (most common)
- Key has expired
- Account has no credits
- Key copied incorrectly

**Solutions:**
1. Make absolutely sure you restarted the server
2. Generate a brand new key from OpenRouter
3. Check your OpenRouter account has credits
4. Double-check the .env file format

### Issue 2: Key Has Whitespace

**Problem:**
The key has spaces, quotes, or newlines in it.

**Fix:**
```bash
# Check for issues
cd /Users/aliyadollahi/Projects/market_analysis/backend
grep OPENROUTER_API_KEY .env

# Should show exactly:
# OPENROUTER_API_KEY=sk-or-v1-... (no spaces, no quotes)
```

### Issue 3: Key Format is Wrong

**Problem:**
Key doesn't start with `sk-or-v1-`

**Fix:**
- Make sure you copied the entire key
- Keys should start with `sk-or-v1-`
- If yours doesn't, get a new one from OpenRouter

### Issue 4: Multiple .env Files

**Problem:**
There might be multiple .env files in different locations.

**Fix:**
Make sure you're editing the correct one:
```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
ls -la .env
```

The server looks for `.env` in the `backend` directory first.

## Testing Your Key

You can test your key directly using curl:

```bash
curl https://openrouter.ai/api/v1/models \
  -H "Authorization: Bearer YOUR_API_KEY_HERE" \
  -H "Content-Type: application/json"
```

**If successful:** You'll see a JSON response with available models  
**If failed:** You'll see a 401 error with error details

## Alternative: Use Fallback Mode

If you can't get the API key working right now, the app will work with fallback content:

- Feed items will show default content
- Learning modules will show error (but you can still see the list)
- Chat will return a helpful message

The app is designed to work even without a valid API key, just with limited features.

## Still Having Issues?

1. **Check OpenRouter Dashboard:**
   - Go to https://openrouter.ai
   - Check your account status
   - Verify you have credits
   - Check if keys are active

2. **Generate a Fresh Key:**
   - Delete old keys
   - Create a completely new one
   - Try again

3. **Check Backend Logs:**
   - Look for error messages when server starts
   - Check what happens when you try to use LLM features

## Summary

✅ Get a new key from https://openrouter.ai/keys  
✅ Update `.env` file with correct format  
✅ **RESTART the backend server**  
✅ Verify key is loaded (check startup logs)  

The 401 error means the key is invalid - you need a valid, active key from OpenRouter.




