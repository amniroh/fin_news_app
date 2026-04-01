#!/bin/bash
# Start Market Analysis Backend Server
# Supports both sandbox and DynamoDB modes

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default to sandbox mode if no argument provided
MODE=${1:-sandbox}

if [ "$MODE" = "sandbox" ]; then
    echo -e "${YELLOW}🔶 Starting server in SANDBOX mode (no AWS required)${NC}"
    export USE_SANDBOX=true
elif [ "$MODE" = "dynamodb" ]; then
    echo -e "${BLUE}🔵 Starting server with DynamoDB${NC}"
    export USE_SANDBOX=false
else
    echo "Usage: ./start_server.sh [sandbox|dynamodb]"
    echo "  sandbox  - Use in-memory storage (default, no AWS needed)"
    echo "  dynamodb  - Use AWS DynamoDB"
    exit 1
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies if needed
if [ ! -f "venv/.dependencies_installed" ]; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
    if [ $? -eq 0 ]; then
        touch venv/.dependencies_installed
    else
        echo -e "${YELLOW}⚠️  Error installing dependencies. Please run: pip install -r requirements.txt${NC}"
        exit 1
    fi
fi

# Check for .env file
if [ ! -f ".env" ]; then
    if [ "$MODE" = "dynamodb" ]; then
        echo -e "${YELLOW}⚠️  Warning: .env file not found. DynamoDB mode requires AWS credentials.${NC}"
        echo "   Create .env file with AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
    else
        echo -e "${YELLOW}⚠️  Warning: .env file not found.${NC}"
        echo "   Creating .env file from template..."
        if [ -f "env.template" ]; then
            cp env.template .env
            echo -e "${YELLOW}   Please edit .env and add your OPENROUTER_API_KEY${NC}"
            echo -e "${YELLOW}   Get your key from: https://openrouter.ai/keys${NC}"
        else
            echo -e "${YELLOW}   Please create .env file with OPENROUTER_API_KEY${NC}"
            echo -e "${YELLOW}   Get your key from: https://openrouter.ai/keys${NC}"
        fi
    fi
fi

# Check if OPENROUTER_API_KEY is set (warn but don't fail - let the app handle it)
if [ -f ".env" ]; then
    if ! grep -q "OPENROUTER_API_KEY=sk-or-v1-" .env 2>/dev/null; then
        echo -e "${YELLOW}⚠️  Warning: OPENROUTER_API_KEY not found in .env file${NC}"
        echo -e "${YELLOW}   The server will start but LLM features won't work.${NC}"
        echo -e "${YELLOW}   Get your key from: https://openrouter.ai/keys${NC}"
    fi
fi

# Start the server
echo -e "${GREEN}Starting FastAPI server...${NC}"
echo "Server will be available at: http://localhost:8000"
echo "API docs will be available at: http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

uvicorn main:app --reload --host 0.0.0.0 --port 8000

