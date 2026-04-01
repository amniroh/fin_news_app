import boto3
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
import logging

logger = logging.getLogger(__name__)

class DatabaseService:
    def __init__(self):
        # Initialize DynamoDB client
        self.dynamodb = boto3.resource('dynamodb', region_name='eu-central-1')
        self.table = self.dynamodb.Table('MarketAnalysisUsers')
    
    async def create_user(self, user_id: str, phone_number: str, is_admin: bool = False) -> Dict[str, Any]:
        """Create a new user in the database with investment profile structure"""
        try:
            current_time = datetime.utcnow().isoformat()
            
            user_data = {
                'user_id': user_id,
                'phone_number': phone_number,
                'created_at': current_time,
                'last_updated': current_time,
                'is_admin': is_admin,
                
                # Basic user info
                'name': None,
                'age': None,
                'income_range': None,
                
                # Onboarding data (investment profile)
                'investment_goals': [],  # ['retirement', 'home', 'emergency', 'education', 'general']
                'time_horizon': None,  # years
                'risk_comfort_level': None,  # 1-5 scale based on scenario questions
                'prior_experience': None,  # 0-3 scale
                'current_investments': [],
                
                # Learning progress
                'completed_modules': [],
                'learning_streak': 0,
                'last_learning_date': None,
                'badges_earned': [],
                
                # Portfolio data
                'portfolio_simulations': [],
                'saved_portfolios': [],
                
                # Feed interactions
                'feed_items_viewed': [],
                'feed_preferences': {},
                
                # Behavioral data
                'emotion_alerts_enabled': True,
                'decision_checkpoints_enabled': True,
                
                # Memory and interests
                'facts': [],
                'inferred_interests': [],
                
                # Interactions and conversations
                'interactions': [],
                'conversation_sessions': [],
                'qa_history': []
            }
            
            self.table.put_item(Item=user_data)
            logger.info(f"Created investment user: {user_id}")
            return user_data
            
        except Exception as e:
            logger.error(f"Error creating user {user_id}: {e}")
            raise
    
    async def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user data by user_id"""
        try:
            response = self.table.get_item(Key={'user_id': user_id})
            return response.get('Item')
        except Exception as e:
            logger.error(f"Error getting user {user_id}: {e}")
            return None
    
    async def get_user_by_phone(self, phone_number: str) -> Optional[Dict[str, Any]]:
        """Get user data by phone number"""
        try:
            response = self.table.query(
                IndexName='PhoneNumberIndex',
                KeyConditionExpression='phone_number = :phone',
                ExpressionAttributeValues={':phone': phone_number}
            )
            
            items = response.get('Items', [])
            return items[0] if items else None
            
        except Exception as e:
            logger.error(f"Error getting user by phone {phone_number}: {e}")
            return None
    
    async def update_user_data(self, user_id: str, updates: Dict[str, Any]) -> bool:
        """Update user data"""
        try:
            from decimal import Decimal
            
            # Add last_updated timestamp
            updates['last_updated'] = datetime.utcnow().isoformat()
            
            # Convert floats to Decimal for DynamoDB compatibility
            def convert_floats(obj):
                if isinstance(obj, float):
                    return Decimal(str(obj))
                elif isinstance(obj, dict):
                    return {k: convert_floats(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [convert_floats(item) for item in obj]
                else:
                    return obj
            
            updates = convert_floats(updates)
            
            # Handle reserved keywords
            expression_attribute_names = {}
            expression_attribute_values = {}
            set_expressions = []
            
            for k, v in updates.items():
                if k in ['name', 'location', 'size', 'type', 'status', 'data', 'region', 'source', 'content', 'id']:
                    attr_name = f"#{k}"
                    expression_attribute_names[attr_name] = k
                    set_expressions.append(f"{attr_name} = :{k}")
                else:
                    set_expressions.append(f"{k} = :{k}")
                expression_attribute_values[f":{k}"] = v
            
            update_expression = "SET " + ", ".join(set_expressions)
            
            update_params = {
                'Key': {'user_id': user_id},
                'UpdateExpression': update_expression,
                'ExpressionAttributeValues': expression_attribute_values
            }
            
            if expression_attribute_names:
                update_params['ExpressionAttributeNames'] = expression_attribute_names
            
            self.table.update_item(**update_params)
            return True
            
        except Exception as e:
            logger.error(f"Error updating user {user_id}: {e}")
            return False
    
    async def save_onboarding_data(self, user_id: str, onboarding_data: Dict[str, Any]) -> bool:
        """Save onboarding data for a user"""
        try:
            return await self.update_user_data(user_id, onboarding_data)
        except Exception as e:
            logger.error(f"Error saving onboarding data for {user_id}: {e}")
            return False
    
    async def add_completed_module(self, user_id: str, module_id: str) -> bool:
        """Mark a learning module as completed"""
        try:
            user = await self.get_user(user_id)
            if not user:
                return False
            
            completed = user.get('completed_modules', [])
            if module_id not in completed:
                completed.append(module_id)
            
            return await self.update_user_data(user_id, {
                'completed_modules': completed
            })
        except Exception as e:
            logger.error(f"Error adding completed module for {user_id}: {e}")
            return False
    
    async def update_learning_streak(self, user_id: str) -> bool:
        """Update user's learning streak"""
        try:
            user = await self.get_user(user_id)
            if not user:
                return False
            
            last_date = user.get('last_learning_date')
            today = datetime.utcnow().date().isoformat()
            
            if last_date == today:
                # Already learned today, no change
                return True
            
            streak = user.get('learning_streak', 0)
            if last_date:
                last_date_obj = datetime.fromisoformat(last_date).date()
                today_obj = datetime.fromisoformat(today).date()
                days_diff = (today_obj - last_date_obj).days
                
                if days_diff == 1:
                    # Consecutive day
                    streak += 1
                else:
                    # Streak broken
                    streak = 1
            else:
                # First learning session
                streak = 1
            
            return await self.update_user_data(user_id, {
                'learning_streak': streak,
                'last_learning_date': today
            })
        except Exception as e:
            logger.error(f"Error updating learning streak for {user_id}: {e}")
            return False
    
    async def add_badge(self, user_id: str, badge_id: str) -> bool:
        """Add a badge to user's collection"""
        try:
            user = await self.get_user(user_id)
            if not user:
                return False
            
            badges = user.get('badges_earned', [])
            if badge_id not in badges:
                badges.append(badge_id)
            
            return await self.update_user_data(user_id, {
                'badges_earned': badges
            })
        except Exception as e:
            logger.error(f"Error adding badge for {user_id}: {e}")
            return False
    
    async def save_portfolio_simulation(self, user_id: str, simulation_data: Dict[str, Any]) -> bool:
        """Save a portfolio simulation"""
        try:
            user = await self.get_user(user_id)
            if not user:
                return False
            
            simulations = user.get('portfolio_simulations', [])
            simulation_data['created_at'] = datetime.utcnow().isoformat()
            simulations.append(simulation_data)
            
            # Keep only last 20 simulations
            if len(simulations) > 20:
                simulations = simulations[-20:]
            
            return await self.update_user_data(user_id, {
                'portfolio_simulations': simulations
            })
        except Exception as e:
            logger.error(f"Error saving portfolio simulation for {user_id}: {e}")
            return False
    
    async def add_interaction(self, user_id: str, interaction_data: Dict[str, Any]) -> bool:
        """Add a new user interaction"""
        try:
            user = await self.get_user(user_id)
            if not user:
                return False
            
            interaction_data['timestamp'] = datetime.utcnow().isoformat()
            interactions = user.get('interactions', [])
            interactions.append(interaction_data)
            
            # Keep only last 100 interactions
            if len(interactions) > 100:
                interactions = interactions[-100:]
            
            return await self.update_user_data(user_id, {
                'interactions': interactions
            })
        except Exception as e:
            logger.error(f"Error adding interaction for {user_id}: {e}")
            return False

# Global instance
db_service = DatabaseService()

