#!/usr/bin/env python3
"""
Test OpenRouter API Key
This script helps verify if your OpenRouter API key is valid
"""

import os
from dotenv import load_dotenv
from pathlib import Path

# Load .env file
current_dir = Path(__file__).parent
dotenv_path = current_dir / '.env'
load_dotenv(dotenv_path=dotenv_path)

api_key = os.getenv("OPENROUTER_API_KEY")

print("=" * 60)
print("OpenRouter API Key Verification")
print("=" * 60)
print()

if not api_key:
    print("❌ ERROR: OPENROUTER_API_KEY not found in environment variables")
    print()
    print("Please check:")
    print("1. .env file exists in backend directory")
    print("2. .env file contains: OPENROUTER_API_KEY=sk-or-v1-...")
    print("3. No spaces around the = sign")
    print()
    exit(1)

print(f"✅ Key found in environment")
print(f"   Length: {len(api_key)} characters")
print(f"   Starts with 'sk-or-v1-': {api_key.startswith('sk-or-v1-')}")
print()

# Check for common issues
issues = []
if ' ' in api_key:
    issues.append("⚠️  Key contains spaces (should be removed)")
if '\n' in api_key or '\r' in api_key:
    issues.append("⚠️  Key contains newlines (should be removed)")
if api_key != api_key.strip():
    issues.append("⚠️  Key has leading/trailing whitespace")
if not api_key.startswith('sk-or-v1-'):
    issues.append("⚠️  Key doesn't start with 'sk-or-v1-' (might be invalid format)")

if issues:
    print("Issues found:")
    for issue in issues:
        print(f"   {issue}")
    print()
    print("💡 Tip: Your key should look like:")
    print("   OPENROUTER_API_KEY=sk-or-v1-abc123def456...")
    print("   (no quotes, no spaces, no newlines)")
    print()
else:
    print("✅ Key format looks good")
    print()

# Test the API key
print("Testing API key with OpenRouter...")
print()

try:
    import requests
    
    response = requests.get(
        "https://openrouter.ai/api/v1/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        timeout=10
    )
    
    if response.status_code == 200:
        print("✅ SUCCESS: API key is valid!")
        print("   You can use OpenRouter API")
        print()
        data = response.json()
        if 'data' in data and len(data['data']) > 0:
            print(f"   Available models: {len(data['data'])} models")
    elif response.status_code == 401:
        print("❌ ERROR: API key is INVALID or EXPIRED")
        print("   Status: 401 Unauthorized")
        print()
        print("Possible reasons:")
        print("1. Key has expired or been revoked")
        print("2. Key is incorrect")
        print("3. Account has no credits")
        print()
        print("💡 Solution:")
        print("1. Go to: https://openrouter.ai/keys")
        print("2. Sign in to your account")
        print("3. Generate a NEW API key")
        print("4. Update your .env file with the new key")
        print("5. Restart your backend server")
        print()
    else:
        print(f"⚠️  Unexpected response: {response.status_code}")
        print(f"   Response: {response.text[:200]}")
        
except ImportError:
    print("⚠️  'requests' library not installed")
    print("   Install it with: pip install requests")
    print()
except Exception as e:
    print(f"❌ Error testing API key: {e}")
    print()
    print("This might be a network issue, but the key format looks OK")
    print()

print("=" * 60)




