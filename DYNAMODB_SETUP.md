# DynamoDB Setup Guide for Market Analysis App

This guide will walk you through setting up DynamoDB in your AWS account and connecting it to the Market Analysis app.

## Prerequisites

- An AWS account (sign up at https://aws.amazon.com if you don't have one)
- AWS CLI installed (optional, but recommended)
- Access to AWS Console

## Step 1: Create DynamoDB Table

### Option A: Using AWS Console (Recommended for beginners)

1. **Log in to AWS Console**
   - Go to https://console.aws.amazon.com
   - Sign in with your AWS account

2. **Navigate to DynamoDB**
   - In the AWS Console search bar, type "DynamoDB"
   - Click on "DynamoDB" service

3. **Create Table**
   - Click the "Create table" button
   - Fill in the following:
     - **Table name**: `MarketAnalysisUsers`
     - **Partition key**: `user_id` (type: String)
     - **Table settings**: Use default settings (On-demand capacity mode is fine for development)
   - Click "Create table"

4. **Wait for Table Creation**
   - Wait until the table status shows "Active" (usually takes 10-30 seconds)

### Option B: Using AWS CLI

If you have AWS CLI installed and configured:

```bash
aws dynamodb create-table \
    --table-name MarketAnalysisUsers \
    --attribute-definitions \
        AttributeName=user_id,AttributeType=S \
        AttributeName=phone_number,AttributeType=S \
    --key-schema \
        AttributeName=user_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region eu-central-1
```

## Step 2: Create Global Secondary Index (GSI)

You need to create an index on `phone_number` to look up users by phone number.

### Using AWS Console:

1. **Go to Your Table**
   - Click on the `MarketAnalysisUsers` table
   - Click on the "Indexes" tab

2. **Create Index**
   - Click "Create index"
   - Fill in:
     - **Partition key**: `phone_number` (type: String)
     - **Index name**: `PhoneNumberIndex`
   - Click "Create index"

3. **Wait for Index Creation**
   - Wait until the index status shows "Active"

### Using AWS CLI:

```bash
aws dynamodb update-table \
    --table-name MarketAnalysisUsers \
    --attribute-definitions \
        AttributeName=user_id,AttributeType=S \
        AttributeName=phone_number,AttributeType=S \
    --global-secondary-index-updates \
        "[{\"Create\":{\"IndexName\":\"PhoneNumberIndex\",\"KeySchema\":[{\"AttributeName\":\"phone_number\",\"KeyType\":\"HASH\"}],\"Projection\":{\"ProjectionType\":\"ALL\"}}}]" \
    --region eu-central-1
```

## Step 3: Set Up AWS Credentials

You need AWS credentials to allow your backend to access DynamoDB.

### Option A: IAM User with DynamoDB Access (Recommended)

1. **Create IAM User**
   - Go to AWS Console → IAM (Identity and Access Management)
   - Click "Users" → "Add users"
   - Username: `market-analysis-backend` (or any name you prefer)
   - Select "Provide user access to the AWS Management Console" (optional)
   - Click "Next"

2. **Set Permissions**
   - Select "Attach policies directly"
   - Search for and select: `AmazonDynamoDBFullAccess` (for development)
   - **OR** create a custom policy with minimal permissions (see below)
   - Click "Next" → "Create user"

3. **Create Access Keys**
   - Click on the user you just created
   - Go to "Security credentials" tab
   - Click "Create access key"
   - Select "Application running outside AWS"
   - Click "Next" → "Create access key"
   - **IMPORTANT**: Copy both the Access Key ID and Secret Access Key
   - Save them securely (you won't be able to see the secret key again)

### Option B: Use AWS Credentials File

If you have AWS CLI configured, you can use your existing credentials:

```bash
# Check your current AWS credentials
cat ~/.aws/credentials
```

### Minimal IAM Policy (Optional - More Secure)

Instead of `AmazonDynamoDBFullAccess`, you can create a custom policy with minimal permissions:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "dynamodb:PutItem",
                "dynamodb:GetItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
                "dynamodb:Scan"
            ],
            "Resource": [
                "arn:aws:dynamodb:eu-central-1:*:table/MarketAnalysisUsers",
                "arn:aws:dynamodb:eu-central-1:*:table/MarketAnalysisUsers/index/*"
            ]
        }
    ]
}
```

## Step 4: Configure Backend Environment Variables

1. **Create `.env` file in backend directory**

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
```

2. **Create `.env` file** (copy from example if it exists, or create new):

```bash
# If .env.example exists
cp .env.example .env

# Or create new file
touch .env
```

3. **Add your AWS credentials to `.env`**:

```env
# OpenRouter API Key
OPENROUTER_API_KEY=your_openrouter_api_key_here

# AWS Credentials
AWS_ACCESS_KEY_ID=your_access_key_id_here
AWS_SECRET_ACCESS_KEY=your_secret_access_key_here
AWS_REGION=eu-central-1

# Optional: Backend URL
BACKEND_URL=http://localhost:8000
```

**Replace:**
- `your_openrouter_api_key_here` with your OpenRouter API key
- `your_access_key_id_here` with the Access Key ID from Step 3
- `your_secret_access_key_here` with the Secret Access Key from Step 3

## Step 5: Verify boto3 Installation

Make sure `boto3` is installed (it should be in requirements.txt):

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
pip install boto3
```

## Step 6: Test the Connection

Create a test script to verify DynamoDB connection:

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
```

Create a test file `test_dynamodb.py`:

```python
import boto3
import os
from dotenv import load_dotenv

load_dotenv()

# Initialize DynamoDB client
dynamodb = boto3.resource(
    'dynamodb',
    region_name=os.getenv('AWS_REGION', 'eu-central-1'),
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
)

try:
    table = dynamodb.Table('MarketAnalysisUsers')
    
    # Test: Get table description
    print(f"✅ Table exists: {table.table_name}")
    print(f"✅ Table status: {table.table_status}")
    print(f"✅ Region: {os.getenv('AWS_REGION', 'eu-central-1')}")
    
    # Test: Try to create a test item
    test_user_id = 'test_user_123'
    table.put_item(Item={
        'user_id': test_user_id,
        'phone_number': '+1234567890',
        'created_at': '2024-01-01T00:00:00',
        'last_updated': '2024-01-01T00:00:00'
    })
    print(f"✅ Successfully created test user: {test_user_id}")
    
    # Test: Retrieve the item
    response = table.get_item(Key={'user_id': test_user_id})
    if 'Item' in response:
        print(f"✅ Successfully retrieved test user")
        print(f"   User ID: {response['Item']['user_id']}")
    
    # Clean up: Delete test item
    table.delete_item(Key={'user_id': test_user_id})
    print(f"✅ Cleaned up test user")
    
    print("\n🎉 DynamoDB connection successful!")
    
except Exception as e:
    print(f"❌ Error: {e}")
    print("\nTroubleshooting:")
    print("1. Check your AWS credentials in .env file")
    print("2. Verify the table name is 'MarketAnalysisUsers'")
    print("3. Check your AWS region matches (eu-central-1)")
    print("4. Verify your IAM user has DynamoDB permissions")
```

Run the test:

```bash
python test_dynamodb.py
```

If successful, you should see:
```
✅ Table exists: MarketAnalysisUsers
✅ Table status: ACTIVE
✅ Region: eu-central-1
✅ Successfully created test user: test_user_123
✅ Successfully retrieved test user
   User ID: test_user_123
✅ Cleaned up test user

🎉 DynamoDB connection successful!
```

## Step 7: Start the Backend

Once the connection is verified:

```bash
cd /Users/aliyadollahi/Projects/market_analysis/backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The backend should start without DynamoDB errors.

## Troubleshooting

### Error: "Unable to locate credentials"

**Solution:**
- Make sure your `.env` file exists in the `backend` directory
- Verify `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are set correctly
- Check that you're loading the `.env` file (the code should do this automatically)

### Error: "ResourceNotFoundException: Requested resource not found"

**Solution:**
- Verify the table name is exactly `MarketAnalysisUsers` (case-sensitive)
- Check that the table exists in the correct region (`eu-central-1`)
- Wait a few minutes after creating the table - it may take time to become available

### Error: "AccessDeniedException"

**Solution:**
- Verify your IAM user has DynamoDB permissions
- Check that the policy is attached to your IAM user
- Try using `AmazonDynamoDBFullAccess` policy for testing

### Error: "ValidationException: One or more parameter values were invalid"

**Solution:**
- Check that the GSI `PhoneNumberIndex` exists
- Verify the table schema matches what the code expects
- Make sure all required attributes are provided when creating users

## Security Best Practices

1. **Never commit `.env` file to git** (it's already in `.gitignore`)
2. **Use IAM roles instead of access keys when possible** (for production)
3. **Rotate access keys regularly**
4. **Use minimal permissions** (custom IAM policy instead of full access)
5. **Enable AWS CloudTrail** to monitor DynamoDB access

## Cost Considerations

- DynamoDB On-Demand pricing: Pay only for what you use
- Free tier: 25 GB storage, 2.5 million read requests, 2.5 million write requests per month
- For development/testing, costs should be minimal or free

## Next Steps

Once DynamoDB is set up and connected:

1. Test the onboarding flow in your app
2. Verify users are being created in DynamoDB
3. Check the AWS Console to see your data
4. Monitor costs in AWS Cost Explorer

## Additional Resources

- [DynamoDB Documentation](https://docs.aws.amazon.com/dynamodb/)
- [boto3 Documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html)
- [AWS IAM Best Practices](https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html)

