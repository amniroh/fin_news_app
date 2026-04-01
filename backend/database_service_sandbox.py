"""
Sandbox Database Service - In-memory storage for testing
Use this instead of DynamoDB when testing locally
"""

import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

class SandboxDatabaseService:
    """
    In-memory database service that mimics DynamoDB interface
    Perfect for local testing without AWS setup
    """
    
    def __init__(self, persist_to_file: bool = False, data_file: Optional[Path] = None):
        """
        Initialize sandbox database
        
        Args:
            persist_to_file: If True, save data to JSON file (survives restarts)
            data_file: Path to JSON file for persistence
        """
        self._users: Dict[str, Dict[str, Any]] = {}
        self._persist_to_file = persist_to_file
        self._data_file = data_file or Path(__file__).parent / "sandbox_data.json"
        
        # Load existing data if file exists
        if persist_to_file and self._data_file.exists():
            try:
                with open(self._data_file, 'r') as f:
                    data = json.load(f)
                    # Handle both old and new file formats
                    if isinstance(data, dict):
                        self._users = data.get('users', {})
                    else:
                        self._users = {}
                # Ensure all users have valid structure
                self._users = {k: v for k, v in self._users.items() if isinstance(v, dict) and 'user_id' in v}
                logger.info(f"✅ Loaded {len(self._users)} users from sandbox data file: {self._data_file}")
            except json.JSONDecodeError as e:
                logger.warning(f"⚠️  Sandbox data file has invalid JSON. Starting fresh. Error: {e}")
                # Backup corrupted file
                try:
                    backup_file = self._data_file.with_suffix('.json.backup')
                    import shutil
                    shutil.copy(self._data_file, backup_file)
                    logger.info(f"⚠️  Backed up corrupted file to {backup_file}")
                except:
                    pass
                self._users = {}
            except Exception as e:
                logger.warning(f"⚠️  Could not load sandbox data from {self._data_file}: {e}. Starting fresh.")
                self._users = {}
    
    def _save_to_file(self):
        """Save current data to file if persistence is enabled"""
        if self._persist_to_file:
            try:
                data = {'users': self._users, 'last_updated': datetime.utcnow().isoformat()}
                # Ensure directory exists
                self._data_file.parent.mkdir(parents=True, exist_ok=True)
                # Write to temporary file first, then rename (atomic operation)
                temp_file = self._data_file.with_suffix('.json.tmp')
                with open(temp_file, 'w') as f:
                    json.dump(data, f, indent=2, default=str)
                # Atomic rename
                temp_file.replace(self._data_file)
                logger.debug(f"💾 Saved {len(self._users)} users to sandbox data file")
            except Exception as e:
                logger.error(f"❌ Could not save sandbox data to {self._data_file}: {e}")
                import traceback
                logger.error(traceback.format_exc())
    
    async def create_user(self, user_id: str, phone_number: str, is_admin: bool = False) -> Dict[str, Any]:
        """Create a new user in the sandbox database"""
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
                'investment_goals': [],
                'time_horizon': None,
                'risk_comfort_level': None,
                'prior_experience': None,
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
            
            self._users[user_id] = user_data
            self._save_to_file()
            logger.info(f"Created sandbox user: {user_id}")
            return user_data
            
        except Exception as e:
            logger.error(f"Error creating sandbox user {user_id}: {e}")
            raise
    
    async def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user data by user_id"""
        try:
            return self._users.get(user_id)
        except Exception as e:
            logger.error(f"Error getting sandbox user {user_id}: {e}")
            return None
    
    async def get_user_by_phone(self, phone_number: str) -> Optional[Dict[str, Any]]:
        """Get user data by phone number"""
        try:
            for user in self._users.values():
                if user.get('phone_number') == phone_number:
                    return user
            return None
        except Exception as e:
            logger.error(f"Error getting sandbox user by phone {phone_number}: {e}")
            return None
    
    async def update_user_data(self, user_id: str, updates: Dict[str, Any]) -> bool:
        """Update user data"""
        try:
            if user_id not in self._users:
                logger.warning(f"User {user_id} not found for update")
                return False
            
            # Add last_updated timestamp
            updates['last_updated'] = datetime.utcnow().isoformat()
            
            # Update user data
            self._users[user_id].update(updates)
            self._save_to_file()
            return True
            
        except Exception as e:
            logger.error(f"Error updating sandbox user {user_id}: {e}")
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
            if user_id not in self._users:
                logger.warning(f"User {user_id} not found when adding completed module. User should exist at this point.")
                return False
            
            user = self._users[user_id]
            completed = user.get('completed_modules', [])
            if module_id not in completed:
                completed.append(module_id)
            
            result = await self.update_user_data(user_id, {
                'completed_modules': completed
            })
            if not result:
                logger.error(f"Failed to update completed modules for user {user_id}")
            return result
        except Exception as e:
            logger.error(f"Error adding completed module for {user_id}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    async def update_learning_streak(self, user_id: str) -> bool:
        """Update user's learning streak"""
        try:
            if user_id not in self._users:
                return False
            
            user = self._users[user_id]
            last_date = user.get('last_learning_date')
            today = datetime.utcnow().date().isoformat()
            
            if last_date == today:
                return True
            
            streak = user.get('learning_streak', 0)
            if last_date:
                last_date_obj = datetime.fromisoformat(last_date).date()
                today_obj = datetime.fromisoformat(today).date()
                days_diff = (today_obj - last_date_obj).days
                
                if days_diff == 1:
                    streak += 1
                else:
                    streak = 1
            else:
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
            if user_id not in self._users:
                return False
            
            user = self._users[user_id]
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
            if user_id not in self._users:
                return False
            
            user = self._users[user_id]
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
            if user_id not in self._users:
                return False
            
            user = self._users[user_id]
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
    
    async def save_user_interaction(self, user_id: str, stage: str, user_message: str, ai_response: str, image_data: str = None, image_filename: str = None) -> bool:
        """Save a user interaction (for compatibility with main.py)"""
        interaction_data = {
            'stage': stage,
            'user_message': user_message,
            'ai_response': ai_response,
        }
        if image_data:
            interaction_data['image_data'] = image_data
            interaction_data['image_filename'] = image_filename
        
        return await self.add_interaction(user_id, interaction_data)
    
    def clear_all_data(self):
        """Clear all sandbox data (useful for testing)"""
        self._users.clear()
        if self._persist_to_file and self._data_file.exists():
            self._data_file.unlink()
        logger.info("Cleared all sandbox data")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about sandbox database"""
        return {
            'total_users': len(self._users),
            'users': list(self._users.keys()),
            'persist_to_file': self._persist_to_file,
            'data_file': str(self._data_file) if self._persist_to_file else None
        }
    
    def reload_from_file(self) -> bool:
        """Reload data from file (useful for testing/debugging)"""
        if not self._persist_to_file or not self._data_file.exists():
            return False
        try:
            with open(self._data_file, 'r') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self._users = data.get('users', {})
                    self._users = {k: v for k, v in self._users.items() if isinstance(v, dict) and 'user_id' in v}
                    logger.info(f"🔄 Reloaded {len(self._users)} users from sandbox data file")
                    return True
        except Exception as e:
            logger.error(f"❌ Error reloading sandbox data: {e}")
            return False
        return False

