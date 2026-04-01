# Quick Setup: OpenRouter API Key

## 3 Simple Steps

### 1. Get Your API Key
- Go to: https://openrouter.ai/keys
- Sign in (or create account)
- Click "Create Key"
- Copy the key (starts with `sk-or-v1-`)

### 2. Create .env File
```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
cp .env.example .env
```

### 3. Add Your Key
Open `.env` and replace `your-api-key-here` with your actual key:

```env
OPENROUTER_API_KEY=sk-or-v1-paste-your-actual-key-here
```

## Done! ✅

Now start the server:
```bash
./start_server.sh sandbox
```

## Need More Help?

See [OPENROUTER_SETUP.md](OPENROUTER_SETUP.md) for detailed instructions and troubleshooting.

