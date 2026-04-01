# Quick Start Guide

## 1. Set Up DynamoDB

Follow the detailed guide: `../DYNAMODB_SETUP.md`

Or quick setup:
1. Create table `MarketAnalysisUsers` in AWS DynamoDB
2. Add GSI `PhoneNumberIndex` on `phone_number`
3. Create IAM user with DynamoDB permissions
4. Get access keys

## 2. Configure Environment

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
cp .env.example .env
# Edit .env and add your credentials
```

Required in `.env`:
- `OPENROUTER_API_KEY`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION=eu-central-1`

## 3. Install Dependencies

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 4. Test DynamoDB Connection

```bash
python test_dynamodb.py
```

Should see: `🎉 All tests passed!`

## 5. Start Backend

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Backend will be available at: http://localhost:8000

## 6. Test API

```bash
# Health check
curl http://localhost:8000/health

# Should return JSON with status: "healthy"
```

## Troubleshooting

- **"Unable to locate credentials"**: Check `.env` file exists and has correct values
- **"ResourceNotFoundException"**: Verify table name is exactly `MarketAnalysisUsers`
- **"AccessDeniedException"**: Check IAM user has DynamoDB permissions

See `DYNAMODB_SETUP.md` for detailed troubleshooting.

