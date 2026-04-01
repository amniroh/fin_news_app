#!/usr/bin/env python3
"""
Test script to verify DynamoDB connection and setup
Run this after setting up your .env file with AWS credentials
"""

import boto3
import os
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables
current_dir = Path(__file__).parent
dotenv_path = current_dir / '.env'

if not dotenv_path.exists():
    for parent in current_dir.parents:
        potential_path = parent / '.env'
        if potential_path.exists():
            dotenv_path = potential_path
            break

load_dotenv(dotenv_path=dotenv_path)

def test_dynamodb_connection():
    """Test DynamoDB connection and basic operations"""
    
    # Get credentials from environment
    aws_access_key = os.getenv('AWS_ACCESS_KEY_ID')
    aws_secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')
    aws_region = os.getenv('AWS_REGION', 'eu-central-1')
    
    # Check if credentials are set
    if not aws_access_key or not aws_secret_key:
        print("❌ Error: AWS credentials not found in .env file")
        print("   Please set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
        return False
    
    print(f"🔍 Testing DynamoDB connection...")
    print(f"   Region: {aws_region}")
    print(f"   Access Key ID: {aws_access_key[:10]}...")
    print()
    
    try:
        # Initialize DynamoDB client
        dynamodb = boto3.resource(
            'dynamodb',
            region_name=aws_region,
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key
        )
        
        # Get table
        table = dynamodb.Table('MarketAnalysisUsers')
        
        # Test 1: Check if table exists
        print("📋 Test 1: Checking if table exists...")
        table.load()
        print(f"   ✅ Table exists: {table.table_name}")
        print(f"   ✅ Table status: {table.table_status}")
        print(f"   ✅ Table ARN: {table.table_arn}")
        print()
        
        # Test 2: Check indexes
        print("📋 Test 2: Checking indexes...")
        indexes = table.global_secondary_indexes or []
        phone_index = [idx for idx in indexes if idx['IndexName'] == 'PhoneNumberIndex']
        
        if phone_index:
            print(f"   ✅ PhoneNumberIndex found")
            print(f"   ✅ Index status: {phone_index[0]['IndexStatus']}")
        else:
            print(f"   ⚠️  PhoneNumberIndex not found - you may need to create it")
        print()
        
        # Test 3: Create a test user
        print("📋 Test 3: Creating test user...")
        test_user_id = f'test_user_{int(__import__("time").time())}'
        test_phone = '+1234567890'
        
        table.put_item(Item={
            'user_id': test_user_id,
            'phone_number': test_phone,
            'created_at': '2024-01-01T00:00:00',
            'last_updated': '2024-01-01T00:00:00',
            'investment_goals': [],
            'completed_modules': [],
            'badges_earned': [],
        })
        print(f"   ✅ Successfully created test user: {test_user_id}")
        print()
        
        # Test 4: Retrieve the user
        print("📋 Test 4: Retrieving test user...")
        response = table.get_item(Key={'user_id': test_user_id})
        if 'Item' in response:
            item = response['Item']
            print(f"   ✅ Successfully retrieved user")
            print(f"      User ID: {item['user_id']}")
            print(f"      Phone: {item.get('phone_number', 'N/A')}")
        else:
            print(f"   ❌ Failed to retrieve user")
            return False
        print()
        
        # Test 5: Query by phone number (if index exists)
        if phone_index:
            print("📋 Test 5: Querying by phone number...")
            try:
                response = table.query(
                    IndexName='PhoneNumberIndex',
                    KeyConditionExpression='phone_number = :phone',
                    ExpressionAttributeValues={':phone': test_phone}
                )
                if response.get('Items'):
                    print(f"   ✅ Successfully queried by phone number")
                    print(f"      Found {len(response['Items'])} user(s)")
                else:
                    print(f"   ⚠️  No users found with that phone number")
            except Exception as e:
                print(f"   ⚠️  Query by phone failed: {e}")
            print()
        
        # Test 6: Update user
        print("📋 Test 6: Updating test user...")
        table.update_item(
            Key={'user_id': test_user_id},
            UpdateExpression='SET last_updated = :now',
            ExpressionAttributeValues={':now': '2024-01-02T00:00:00'}
        )
        print(f"   ✅ Successfully updated user")
        print()
        
        # Test 7: Clean up
        print("📋 Test 7: Cleaning up test user...")
        table.delete_item(Key={'user_id': test_user_id})
        print(f"   ✅ Successfully deleted test user")
        print()
        
        print("=" * 50)
        print("🎉 All tests passed! DynamoDB is properly configured.")
        print("=" * 50)
        return True
        
    except Exception as e:
        print("=" * 50)
        print(f"❌ Error: {e}")
        print("=" * 50)
        print("\n🔧 Troubleshooting:")
        print("1. Check your AWS credentials in .env file")
        print("2. Verify the table name is exactly 'MarketAnalysisUsers'")
        print("3. Check your AWS region matches (eu-central-1)")
        print("4. Verify your IAM user has DynamoDB permissions")
        print("5. Make sure the table exists in the correct region")
        return False

if __name__ == "__main__":
    success = test_dynamodb_connection()
    exit(0 if success else 1)

