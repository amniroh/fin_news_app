#!/usr/bin/env python3
"""
Start Market Analysis Backend Server
Supports both sandbox and DynamoDB modes
"""

import os
import sys
import subprocess
from pathlib import Path

def main():
    # Default to sandbox mode
    mode = sys.argv[1] if len(sys.argv) > 1 else "sandbox"
    
    if mode not in ["sandbox", "dynamodb"]:
        print("Usage: python start_server.py [sandbox|dynamodb]")
        print("  sandbox  - Use in-memory storage (default, no AWS needed)")
        print("  dynamodb  - Use AWS DynamoDB")
        sys.exit(1)
    
    # Set environment variable
    os.environ["USE_SANDBOX"] = "true" if mode == "sandbox" else "false"
    
    if mode == "sandbox":
        print("🔶 Starting server in SANDBOX mode (no AWS required)")
    else:
        print("🔵 Starting server with DynamoDB")
        # Check for .env file
        env_file = Path(__file__).parent / ".env"
        if not env_file.exists():
            print("⚠️  Warning: .env file not found. DynamoDB mode requires AWS credentials.")
            print("   Create .env file with AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
    
    # Start uvicorn
    try:
        import uvicorn
        uvicorn.run(
            "main:app",
            host="0.0.0.0",
            port=8000,
            reload=True
        )
    except ImportError:
        print("Error: uvicorn not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

if __name__ == "__main__":
    main()

