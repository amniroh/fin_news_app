# Market Analysis - Investment Education App

An investment education app designed to help users with limited financial knowledge make healthy and sustainable investment decisions. The app provides personalized, bite-sized education, portfolio simulations, and behavioral guidance.

## Features

### Core Features

1. **Simple Onboarding**
   - Age, income range, goals, time horizon
   - Risk comfort assessment (scenario-based questions)
   - Prior experience level

2. **Visual Goal Builder**
   - Set investment goals (Retirement, Home, Emergency Fund, Education, General)
   - Personalized investment suggestions
   - Visual timeline and projections

3. **Bite-Sized Learning Modules**
   - 30-60 second micro-lessons
   - Topics: Stocks, Risk, Diversification, Compound Interest, ETFs, Common Mistakes
   - Gamification: Streaks, quizzes, badges

4. **Personalized Investment Feed**
   - Market updates in plain English
   - Educational concepts
   - Common mistakes to avoid
   - Portfolio updates
   - Investor psychology tips

5. **Portfolio Simulations**
   - Historical performance simulations
   - "What if" scenarios
   - Visual growth charts
   - Risk/return comparisons

6. **Emotion-Control Alerts**
   - Reassuring notifications during market volatility
   - Long-term perspective reminders
   - Behavioral safety nets

7. **Plain-English Explanations**
   - No jargon
   - Simple analogies
   - Context-driven education

8. **Safe Decision Checkpoints**
   - Warnings for risky moves
   - Educational nudges
   - Historical context

9. **Investment Product Breakdown Cards**
   - One-sentence definitions
   - When to use
   - Pros/cons
   - Risk meters
   - Historical comparisons

10. **Q&A Chat**
    - Ask investment questions
    - Expert-verified answers
    - Simple explanations with analogies

## Tech Stack

### Backend
- **FastAPI** - Python web framework
- **DynamoDB** - User data storage (or Sandbox mode for testing)
- **OpenRouter** - LLM integration for personalized content
- **yfinance** - Market data
- **pandas/numpy** - Data analysis

### Frontend
- **Flutter** - Cross-platform mobile framework
- **fl_chart** - Data visualization
- **shared_preferences** - Local storage

## Quick Start

### Option 1: Sandbox Mode (No AWS Setup Required) ⭐ Recommended for Testing

Perfect for testing and demos without AWS setup:

```bash
cd backend
./start_server.sh sandbox
```

Or using Python:
```bash
cd backend
python start_server.py sandbox
```

See [SANDBOX_MODE.md](SANDBOX_MODE.md) for details.

### Option 2: DynamoDB Mode (Production)

Requires AWS setup:

1. **Set up DynamoDB** - See [DYNAMODB_SETUP.md](DYNAMODB_SETUP.md)
2. **Configure environment** - Create `.env` file with AWS credentials
3. **Start server:**
   ```bash
   cd backend
   ./start_server.sh dynamodb
   ```

## Project Structure

```
market_analysis/
├── backend/
│   ├── main.py                      # FastAPI application
│   ├── database_service.py          # DynamoDB service
│   ├── database_service_sandbox.py  # Sandbox (in-memory) service
│   ├── start_server.sh              # Startup script (sandbox/dynamodb)
│   ├── start_server.py              # Python startup script
│   ├── requirements.txt              # Python dependencies
│   └── .env                         # Environment variables
├── flutter_app/
│   ├── lib/
│   │   ├── main.dart                # App entry point
│   │   ├── services/                # API and user services
│   │   ├── screens/                  # UI screens
│   │   ├── models/                  # Data models
│   │   └── widgets/                 # Reusable widgets
│   └── pubspec.yaml                 # Flutter dependencies
├── DYNAMODB_SETUP.md                # DynamoDB setup guide
├── SANDBOX_MODE.md                  # Sandbox mode guide
└── README.md
```

## Setup Instructions

### Backend Setup

1. Navigate to the backend directory:
```bash
cd backend
```

2. Create a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. **Set up OpenRouter API Key:**
   - Get API key from https://openrouter.ai/keys
   - Create `.env` file in backend directory
   - Add: `OPENROUTER_API_KEY=your-api-key-here`
   - See [OPENROUTER_SETUP.md](OPENROUTER_SETUP.md) for detailed instructions

5. **For Sandbox Mode (Testing):**
   ```bash
   ./start_server.sh sandbox
   ```
   No AWS setup needed!

6. **For DynamoDB Mode (Production):**
   - **See detailed instructions in [DYNAMODB_SETUP.md](DYNAMODB_SETUP.md)**
   - Quick summary:
     - Create table `MarketAnalysisUsers` with primary key `user_id` (String)
     - Add Global Secondary Index `PhoneNumberIndex` on `phone_number`
     - Set up AWS credentials in `.env` file
   - Start server:
     ```bash
     ./start_server.sh dynamodb
     ```

### Frontend Setup

1. Navigate to the Flutter app directory:
```bash
cd flutter_app
```

2. Install Flutter dependencies:
```bash
flutter pub get
```

3. Update the backend URL in `lib/services/api_service.dart`:
   - Set `prodUrl` to your deployed backend URL
   - Or use `localUrl` for local development

4. Run the app:
```bash
flutter run
```

## API Endpoints

### Onboarding
- `POST /onboarding` - Save user onboarding data

### Learning
- `GET /learning/modules` - Get list of learning modules
- `GET /learning/modules/{module_id}` - Get module content

### Portfolio
- `POST /portfolio/simulate` - Run portfolio simulation

### Feed
- `POST /feed/items` - Get personalized feed items

### Chat
- `POST /chat` - Ask investment questions

### User
- `GET /user/{user_id}` - Get user profile
- `GET /user/{user_id}/progress` - Get learning progress
- `GET /health` - Health check (shows database mode)

## Design Principles

1. **Cognitive Load Reduction** - One decision at a time, no dense dashboards
2. **Mental Models** - Use analogies (ETFs = fruit baskets)
3. **Nudging, Not Telling** - Gentle guidance
4. **Clear Data Visualization** - Simple charts, timeline bars, donut charts
5. **Emotional Safety** - Reassure users during volatility

## User Journey

1. User downloads app → Welcome screen
2. Set investment goals → Goal selection
3. Choose comfort level → Scenario-based risk assessment
4. App suggests plan → Personalized recommendations
5. User explores → Feed, learning modules, simulations
6. Daily feed teaches → Small, digestible lessons

## Development Modes

### Sandbox Mode (Testing)
- ✅ No AWS setup required
- ✅ In-memory database
- ✅ Data persists to JSON file
- ✅ Perfect for development

### DynamoDB Mode (Production)
- ✅ Production-ready
- ✅ Scalable
- ✅ AWS managed
- ✅ Requires AWS setup

## Future Enhancements

- Push notifications for emotion-control alerts
- Community Q&A with expert verification
- More detailed portfolio tracking
- Integration with real investment accounts (read-only)
- Advanced analytics and insights
- Multi-language support

## License

This project is for educational purposes.
