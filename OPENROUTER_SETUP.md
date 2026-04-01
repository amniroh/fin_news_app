# OpenRouter API Key Setup Guide

OpenRouter is used to power the LLM (Large Language Model) features in the Market Analysis app, including:
- Personalized investment suggestions
- Learning module content generation
- Feed item generation (market updates, concepts, tips)
- Chat/Q&A responses

## Step 1: Get Your OpenRouter API Key

1. **Sign up for OpenRouter**
   - Go to https://openrouter.ai/
   - Click "Sign In" or "Get Started"
   - Create an account (you can use Google/GitHub to sign in quickly)

2. **Navigate to API Keys**
   - Once logged in, go to: https://openrouter.ai/keys
   - Or click on your profile → "Keys" in the navigation

3. **Create a New API Key**
   - Click "Create Key" button
   - Give it a name (e.g., "Market Analysis App")
   - Copy the API key immediately (you won't be able to see it again!)
   - Format: `sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

## Step 2: Add API Key to Your Project

### Option A: Create .env File (Recommended)

1. **Navigate to backend directory:**
   ```bash
   cd /Users/aliyadollahi/Projects/market_analysis/backend
   ```

2. **Create .env file:**
   ```bash
   touch .env
   ```

3. **Add your API key:**
   ```bash
   # Open the file in your editor
   nano .env
   # or
   code .env  # if using VS Code
   ```

4. **Add this line to .env:**
   ```env
   OPENROUTER_API_KEY=sk-or-v1-your-actual-api-key-here
   ```

   Replace `sk-or-v1-your-actual-api-key-here` with your actual API key from Step 1.

### Option B: Export as Environment Variable

For temporary testing:
```bash
export OPENROUTER_API_KEY=sk-or-v1-your-actual-api-key-here
```

**Note:** This only works for the current terminal session. Use `.env` file for permanent setup.

## Step 3: Verify the Setup

### Test 1: Check if .env is loaded

Start the server and check the logs:
```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
./start_server.sh sandbox
```

Look for:
- ✅ No error about missing API key
- ✅ Server starts successfully

If you see:
```
⚠️  OPENROUTER_API_KEY not found in environment variables
ValueError: OPENROUTER_API_KEY not found in environment variables
```

Then the API key is not being loaded correctly.

### Test 2: Test the API

Once the server is running, test an endpoint that uses the LLM:

```bash
# Test the chat endpoint
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test_user",
    "message": "What is a stock?"
  }'
```

You should get a response with investment education content.

### Test 3: Check Health Endpoint

```bash
curl http://localhost:8000/health
```

Should return:
```json
{
  "status": "healthy",
  "service": "Market Analysis Backend",
  "version": "1.0.0",
  "database": "sandbox",
  "timestamp": "..."
}
```

## Step 4: Complete .env File Example

For reference, here's a complete `.env` file example:

```env
# OpenRouter API Key (Required for LLM features)
OPENROUTER_API_KEY=sk-or-v1-your-actual-api-key-here

# Database Mode (optional, defaults to false/DynamoDB)
USE_SANDBOX=true

# AWS Credentials (only needed if USE_SANDBOX=false)
# AWS_ACCESS_KEY_ID=your_aws_access_key
# AWS_SECRET_ACCESS_KEY=your_aws_secret_key
# AWS_REGION=eu-central-1
```

## Troubleshooting

### Problem: "OPENROUTER_API_KEY not found"

**Solutions:**
1. Check that `.env` file exists in `backend/` directory
2. Verify the file is named exactly `.env` (not `.env.txt` or `env`)
3. Check that the API key line starts with `OPENROUTER_API_KEY=` (no spaces)
4. Make sure there are no quotes around the API key value
5. Restart the server after creating/modifying `.env`

### Problem: "Invalid API key" or 401 errors

**Solutions:**
1. Verify you copied the entire API key (they're long!)
2. Check for extra spaces or newlines
3. Make sure you're using the key from https://openrouter.ai/keys
4. Check if your OpenRouter account has credits/balance

### Problem: API key works but responses are slow

**Solutions:**
1. This is normal - LLM responses take a few seconds
2. The app uses `gpt-4o-mini` which is fast and cost-effective
3. For faster responses, you could use a faster model (modify in `main.py`)

### Problem: Rate limiting errors

**Solutions:**
1. OpenRouter has rate limits based on your plan
2. Free tier has lower limits
3. Consider upgrading your OpenRouter plan for production use
4. Add retry logic or caching if needed

## Security Best Practices

1. **Never commit .env to git** ✅ (already in `.gitignore`)
2. **Don't share your API key** - treat it like a password
3. **Rotate keys periodically** - create new keys and delete old ones
4. **Use different keys for dev/prod** - separate environments
5. **Monitor usage** - check OpenRouter dashboard for usage/charges

## OpenRouter Pricing

- **Free tier**: Limited requests per day
- **Pay-as-you-go**: Pay per token used
- **Check pricing**: https://openrouter.ai/docs/pricing

The app uses `gpt-4o-mini` which is one of the most cost-effective models.

## Alternative: Using OpenAI Directly

If you prefer to use OpenAI directly instead of OpenRouter:

1. Get OpenAI API key from https://platform.openai.com/api-keys
2. Modify `main.py` to use OpenAI directly:
   ```python
   # Change from:
   llm_client = OpenAI(
       api_key=api_key,
       base_url="https://openrouter.ai/api/v1"
   )
   
   # To:
   llm_client = OpenAI(api_key=api_key)
   ```
3. Update model names (remove `openai/` prefix)

## Next Steps

Once your API key is set up:

1. ✅ Start the server: `./start_server.sh sandbox`
2. ✅ Test the health endpoint
3. ✅ Try the chat endpoint
4. ✅ Test onboarding (generates personalized suggestions)
5. ✅ Try learning modules (generates educational content)

Your app is now ready to use LLM-powered features! 🎉

