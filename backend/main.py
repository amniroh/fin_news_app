#!/usr/bin/env python3
"""
Market Analysis Backend - Investment Education App
A FastAPI backend for helping users make healthy and sustainable investment decisions
"""

import os
import json
import logging
import asyncio
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path
from dotenv import load_dotenv

# Find and load .env file
current_dir = Path(__file__).parent
dotenv_path = current_dir / '.env'

if not dotenv_path.exists():
    for parent in current_dir.parents:
        potential_path = parent / '.env'
        if potential_path.exists():
            dotenv_path = potential_path
            break

load_dotenv(dotenv_path=dotenv_path)

# Set up logging first
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Choose database service based on environment variable
USE_SANDBOX = os.getenv("USE_SANDBOX", "false").lower() == "true"

if USE_SANDBOX:
    logger.info("🔶 Using SANDBOX database (in-memory storage)")
    from database_service_sandbox import SandboxDatabaseService
    db_service = SandboxDatabaseService(persist_to_file=True)
else:
    logger.info("🔵 Using DynamoDB (AWS)")
    from database_service import db_service

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yfinance as yf
import pandas as pd
import numpy as np

# Value-metrics web app API
from pathlib import Path as _Path
from value_metrics_api import build_value_router

# Import unified LLM service
from llm_service import llm_service

app = FastAPI(title="Market Analysis API", description="Investment Education API")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount value-metrics router (watchlists, metrics, alerts).
_vm_db = _Path(os.getenv("VALUE_METRICS_DB_PATH", "backend/data/value_metrics.sqlite")).expanduser()
_vm_cache_ttl = int(os.getenv("VALUE_METRICS_CACHE_TTL_SECONDS", "1800"))
value_router = build_value_router(db_path=_vm_db, cache_ttl_seconds=_vm_cache_ttl)
app.include_router(value_router)


@app.on_event("startup")
async def _vm_startup() -> None:
    # Start the alert monitor loop (threshold-crossing detection).
    try:
        stop_evt = getattr(value_router.state, "_stop_evt", None)
        loop_fn = getattr(value_router.state, "_alert_loop", None)
        if stop_evt is not None and loop_fn is not None:
            value_router.state._task = asyncio.create_task(loop_fn(stop_evt))
    except Exception:
        pass


@app.on_event("shutdown")
async def _vm_shutdown() -> None:
    try:
        stop_evt = getattr(value_router.state, "_stop_evt", None)
        task = getattr(value_router.state, "_task", None)
        if stop_evt is not None:
            stop_evt.set()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                pass
    except Exception:
        pass

# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    db_info = "sandbox" if USE_SANDBOX else "dynamodb"
    response = {
        "status": "healthy",
        "service": "Market Analysis Backend",
        "version": "1.0.0",
        "database": db_info,
        "timestamp": datetime.now().isoformat()
    }
    
    # Add sandbox stats if using sandbox
    if USE_SANDBOX and hasattr(db_service, 'get_stats'):
        response["sandbox_stats"] = db_service.get_stats()
    
    # Add LLM provider info
    response["llm_provider"] = llm_service.get_provider_info()
    
    return response

# Helper function for LLM calls
async def call_llm(system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: Optional[int] = None) -> str:
    """Unified LLM call helper"""
    if not llm_service.is_available():
        raise Exception("LLM service is not available")
    return await llm_service.generate_text(system_prompt, user_prompt, temperature, max_tokens)

# Helper function to ensure user exists (for sandbox mode compatibility)
async def ensure_user_exists(user_id: str) -> Dict:
    """
    Get user if exists, otherwise create a new user with default values.
    This ensures sandbox mode works seamlessly - users are auto-created as needed.
    Mimics DynamoDB behavior where users should exist from onboarding, but handles edge cases.
    """
    user = await db_service.get_user(user_id)
    if not user:
        # Auto-create user with default values for sandbox mode
        # In production/DynamoDB, this handles cases where user wasn't created during onboarding
        logger.info(f"Auto-creating user {user_id} (sandbox mode compatibility)")
        await db_service.create_user(user_id, f"user_{user_id}", False)
        user = await db_service.get_user(user_id)
        if not user:
            raise HTTPException(status_code=500, detail="Failed to create user")
    return user

# Pydantic models
class OnboardingRequest(BaseModel):
    user_id: str
    age: Optional[int] = None
    income_range: Optional[str] = None
    investment_goals: List[str] = []
    time_horizon: Optional[int] = None
    risk_comfort_level: Optional[int] = None
    prior_experience: Optional[int] = None

class ChatMessage(BaseModel):
    user_id: str
    message: str
    context: Optional[str] = None

class PortfolioSimulationRequest(BaseModel):
    user_id: str
    monthly_investment: float
    years: int
    asset_allocation: Dict[str, float]  # e.g., {"stocks": 0.7, "bonds": 0.3}
    start_date: Optional[str] = None

class FeedItemRequest(BaseModel):
    user_id: str
    item_type: str  # "market_update", "concept", "mistake", "portfolio_update", "psychology"
    limit: int = 10

# Onboarding endpoints
@app.post("/onboarding")
async def save_onboarding(onboarding: OnboardingRequest):
    """Save user onboarding data"""
    try:
        user = await db_service.get_user(onboarding.user_id)
        if not user:
            # Create new user if doesn't exist
            await db_service.create_user(onboarding.user_id, f"user_{onboarding.user_id}", False)
        
        await db_service.save_onboarding_data(onboarding.user_id, {
            "age": onboarding.age,
            "income_range": onboarding.income_range,
            "investment_goals": onboarding.investment_goals,
            "time_horizon": onboarding.time_horizon,
            "risk_comfort_level": onboarding.risk_comfort_level,
            "prior_experience": onboarding.prior_experience
        })
        
        # Generate personalized investment plan suggestion
        suggestion = await generate_investment_suggestion(onboarding)
        
        return {
            "success": True,
            "message": "Onboarding data saved successfully",
            "suggestion": suggestion
        }
    except Exception as e:
        logger.error(f"Error saving onboarding: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def generate_investment_suggestion(onboarding: OnboardingRequest) -> Dict:
    """Generate personalized investment suggestion based on onboarding data"""
    if not llm_service.is_available():
        # Return default suggestion if LLM is not available
        return {
            "suggested_monthly_investment": 200,
            "recommended_allocation": {"stocks": 0.7, "bonds": 0.2, "cash": 0.1},
            "explanation": "Based on your profile, we suggest starting with a balanced approach. This is a good starting point for beginners.",
            "expected_timeline": f"With consistent investing, you could reach your goals in approximately {onboarding.time_horizon or 10} years."
        }
    
    try:
        prompt = f"""Based on the following user profile, provide a simple, personalized investment suggestion:

Age: {onboarding.age}
Income Range: {onboarding.income_range}
Goals: {', '.join(onboarding.investment_goals)}
Time Horizon: {onboarding.time_horizon} years
Risk Comfort: {onboarding.risk_comfort_level}/5
Experience: {onboarding.prior_experience}/3

Provide a JSON response with:
- suggested_monthly_investment: suggested monthly amount
- recommended_allocation: {{"stocks": X, "bonds": Y, "cash": Z}} (percentages)
- explanation: simple 2-3 sentence explanation in plain English
- expected_timeline: how long to reach their goals

Keep it simple and beginner-friendly. No jargon."""

        system_prompt = "You are a friendly investment education assistant. Provide simple, clear advice in JSON format."
        content = await call_llm(system_prompt, prompt, temperature=0.7, max_tokens=500)
        # Try to parse JSON from response
        try:
            # Extract JSON if wrapped in markdown
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            
            return json.loads(content)
        except:
            # Fallback if JSON parsing fails
            return {
                "suggested_monthly_investment": 200,
                "recommended_allocation": {"stocks": 0.7, "bonds": 0.2, "cash": 0.1},
                "explanation": "Based on your profile, we suggest starting with a balanced approach. This is a good starting point for beginners.",
                "expected_timeline": f"With consistent investing, you could reach your goals in approximately {onboarding.time_horizon or 10} years."
            }
    except Exception as e:
        logger.error(f"Error generating suggestion: {e}")
        return {
            "suggested_monthly_investment": 200,
            "recommended_allocation": {"stocks": 0.7, "bonds": 0.2, "cash": 0.1},
            "explanation": "We recommend starting with a balanced portfolio. This is a safe approach for beginners.",
            "expected_timeline": "With consistent investing, you can make steady progress toward your goals."
        }

# Learning modules endpoints
@app.get("/learning/modules")
async def get_learning_modules():
    """Get list of available learning modules"""
    modules = [
        {
            "id": "what_is_stock",
            "title": "What is a Stock?",
            "duration": 60,
            "difficulty": "beginner",
            "description": "Learn the basics of what stocks are and how they work"
        },
        {
            "id": "risk_time_horizon",
            "title": "Why Risk Decreases with Time",
            "duration": 90,
            "difficulty": "beginner",
            "description": "Understand how time can be your friend in investing"
        },
        {
            "id": "diversification",
            "title": "Why Diversification Matters",
            "duration": 75,
            "difficulty": "beginner",
            "description": "Learn why not putting all eggs in one basket is smart"
        },
        {
            "id": "compound_interest",
            "title": "The Magic of Compound Interest",
            "duration": 60,
            "difficulty": "beginner",
            "description": "See how your money can grow over time"
        },
        {
            "id": "index_funds",
            "title": "Index Funds vs Individual Stocks",
            "duration": 90,
            "difficulty": "intermediate",
            "description": "Compare different investment approaches"
        },
        {
            "id": "avoiding_mistakes",
            "title": "Common Investment Mistakes",
            "duration": 60,
            "difficulty": "beginner",
            "description": "Learn what mistakes to avoid as a beginner"
        }
    ]
    return {"modules": modules}

@app.get("/learning/modules/{module_id}")
async def get_module_content(module_id: str, user_id: str):
    """Get content for a specific learning module"""
    try:
        # Ensure user exists (auto-create in sandbox mode if needed)
        # user_id is required - frontend always provides it
        logger.info(f"Getting module content for module_id={module_id}, user_id={user_id}")
        user = await ensure_user_exists(user_id)
        logger.info(f"User {user_id} exists, proceeding with module generation")
        
        if not llm_service.is_available():
            raise HTTPException(
                status_code=503,
                detail="LLM service not available. Please set GEMINI_API_KEY or OPENROUTER_API_KEY in .env file"
            )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error ensuring user exists for module {module_id}, user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error initializing user session: {str(e)}")
    
    try:
        # Generate personalized module content using LLM
        prompt = f"""Create a 60-90 second educational module about: {module_id}

Make it:
- Simple and beginner-friendly
- Use analogies and examples
- No jargon
- Engaging and conversational
- Include a simple quiz question at the end

Format as JSON with:
- title: module title
- content: main educational content (plain text, conversational)
- analogy: a simple analogy to help understand
- quiz_question: a simple multiple choice question
- quiz_options: array of 4 options
- correct_answer: index of correct answer (0-3)
- key_takeaway: one sentence summary"""

        try:
            system_prompt = "You are a friendly investment educator. Create simple, engaging educational content in JSON format."
            content = await call_llm(system_prompt, prompt, temperature=0.7, max_tokens=800)
        except Exception as api_error:
            # Handle API errors gracefully
            error_str = str(api_error)
            if "401" in error_str or "Unauthorized" in error_str or "authentication" in error_str.lower():
                logger.error(f"LLM API authentication failed: {api_error}")
                logger.error("Please check your GEMINI_API_KEY or OPENROUTER_API_KEY in .env file")
                raise HTTPException(
                    status_code=503,
                    detail="LLM service authentication failed. Please check your API key in .env file."
                )
            else:
                # Re-raise other errors
                raise
        
        # Parse the content
        try:
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            
            module_data = json.loads(content)
            module_data["id"] = module_id
            
            # Update progress tracking (user already ensured to exist above)
            try:
                # Mark as completed for user
                await db_service.add_completed_module(user_id, module_id)
                await db_service.update_learning_streak(user_id)
                
                # Refresh user data to get updated completed_modules count
                user = await db_service.get_user(user_id)
                completed_count = len(user.get('completed_modules', [])) if user else 0
                if completed_count >= 5:
                    await db_service.add_badge(user_id, "learner_5")
                if completed_count >= 10:
                    await db_service.add_badge(user_id, "learner_10")
            except Exception as progress_error:
                # Don't fail the whole request if progress tracking fails
                logger.warning(f"Error updating progress for user {user_id}: {progress_error}")
            
            return module_data
        except Exception as e:
            logger.error(f"Error parsing module content: {e}")
            raise HTTPException(status_code=500, detail="Error generating module content")
            
    except HTTPException:
        # Re-raise HTTP exceptions (like our 503 for auth errors)
        raise
    except Exception as e:
        logger.error(f"Error getting module content: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error generating module content: {str(e)}")

# Portfolio simulation endpoints
@app.post("/portfolio/simulate")
async def simulate_portfolio(request: PortfolioSimulationRequest):
    """Simulate portfolio performance based on historical data"""
    try:
        # Ensure user exists (auto-create in sandbox mode if needed)
        await ensure_user_exists(request.user_id)
        
        # Use S&P 500 for stocks, 10-year Treasury for bonds
        end_date = datetime.now()
        start_date = datetime(end_date.year - request.years, end_date.month, end_date.day)
        
        # Get historical data using Ticker (more reliable than download for single tickers)
        try:
            logger.info(f"Fetching market data from {start_date} to {end_date}")
            stocks_ticker = yf.Ticker("^GSPC")
            stocks_data = stocks_ticker.history(start=start_date, end=end_date)
            
            if stocks_data.empty:
                raise HTTPException(status_code=500, detail="Could not fetch stock data. Try a shorter time period.")
            
            # Verify Close column exists and contains numeric data
            if 'Close' not in stocks_data.columns:
                logger.error(f"Stocks columns: {list(stocks_data.columns)}")
                raise HTTPException(status_code=500, detail="Stock data format error - missing Close price column")
            
            # Ensure Close prices are numeric
            stocks_data['Close'] = pd.to_numeric(stocks_data['Close'], errors='coerce')
            if stocks_data['Close'].isna().all():
                raise HTTPException(status_code=500, detail="Stock price data is invalid - all values are NaN")
            
            logger.info(f"Successfully fetched {len(stocks_data)} days of stock data")
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching market data: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Could not fetch historical market data: {str(e)}")
        
        # Calculate returns
        stocks_returns = stocks_data['Close'].pct_change().dropna()
        
        if len(stocks_returns) == 0:
            raise HTTPException(status_code=500, detail="No valid return data calculated. Try a longer time period.")
        # For bonds (TNX is a yield, not price), convert yield to return approximation
        # Higher yield = lower bond prices (inverse relationship)
        # Use a simplified bond return model: approximate 3% annual bond return
        bonds_returns = pd.Series([0.0025] * len(stocks_returns), index=stocks_returns.index)  # ~3% annual / 12 months / trading days
        
        # Align returns to same dates (use stocks index as base)
        bonds_returns = bonds_returns.reindex(stocks_returns.index, fill_value=0.0025)
        
        # Combine based on allocation
        stock_weight = request.asset_allocation.get("stocks", 0.7)
        bond_weight = request.asset_allocation.get("bonds", 0.3)
        portfolio_returns = (stocks_returns * stock_weight + bonds_returns * bond_weight)
        
        # Ensure no NaN or infinite values
        portfolio_returns = portfolio_returns.replace([np.inf, -np.inf], 0).fillna(0)
        
        # Simulate monthly investments
        monthly_amount = float(request.monthly_investment)
        total_invested = 0.0
        portfolio_value = 0.0
        monthly_values = []
        
        # Convert to list and iterate safely
        returns_list = portfolio_returns.values if hasattr(portfolio_returns, 'values') else list(portfolio_returns)
        
        for i, ret in enumerate(returns_list):
            if i % 21 == 0:  # Approximately monthly (21 trading days)
                total_invested += monthly_amount
                # Ensure return is a valid number (not NaN or string)
                try:
                    ret_float = float(ret) if not (pd.isna(ret) if hasattr(pd, 'isna') else (ret != ret)) else 0.0
                except (ValueError, TypeError):
                    ret_float = 0.0
                
                portfolio_value = (portfolio_value + monthly_amount) * (1 + ret_float)
                monthly_values.append({
                    "month": i // 21,
                    "invested": float(total_invested),
                    "value": float(portfolio_value),
                    "return": float(portfolio_value - total_invested)
                })
        
        # Calculate summary statistics
        final_value = monthly_values[-1]["value"] if monthly_values else monthly_amount
        total_return = final_value - total_invested
        return_percentage = (total_return / total_invested * 100) if total_invested > 0 else 0
        
        # Save simulation
        simulation_data = {
            "monthly_investment": monthly_amount,
            "years": request.years,
            "asset_allocation": request.asset_allocation,
            "final_value": final_value,
            "total_invested": total_invested,
            "total_return": total_return,
            "return_percentage": return_percentage
        }
        await db_service.save_portfolio_simulation(request.user_id, simulation_data)
        
        return {
            "success": True,
            "simulation": {
                "monthly_values": monthly_values,
                "summary": {
                    "total_invested": total_invested,
                    "final_value": final_value,
                    "total_return": total_return,
                    "return_percentage": round(return_percentage, 2)
                }
            }
        }
        
    except Exception as e:
        logger.error(f"Error simulating portfolio: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Personalized feed endpoints
@app.post("/feed/items")
async def get_feed_items(request: FeedItemRequest):
    """Get personalized feed items for user"""
    try:
        logger.info(f"Getting feed items for user_id={request.user_id}, item_type={request.item_type}")
        # Ensure user exists (auto-create in sandbox mode if needed)
        user = await ensure_user_exists(request.user_id)
        logger.info(f"User {request.user_id} exists, generating feed items")
        
        # Generate personalized feed items
        feed_items = []
        
        # Market update
        if request.item_type == "market_update" or request.item_type == "all":
            try:
                market_item = await generate_market_update()
                feed_items.append(market_item)
            except Exception as e:
                logger.warning(f"Error generating market update: {e}, skipping")
        
        # Educational concept
        if request.item_type == "concept" or request.item_type == "all":
            try:
                concept_item = await generate_concept_item(user)
                feed_items.append(concept_item)
            except Exception as e:
                logger.warning(f"Error generating concept item: {e}, skipping")
        
        # Common mistake
        if request.item_type == "mistake" or request.item_type == "all":
            try:
                mistake_item = await generate_mistake_item()
                feed_items.append(mistake_item)
            except Exception as e:
                logger.warning(f"Error generating mistake item: {e}, skipping")
        
        # Portfolio update (if user has portfolio)
        if request.item_type == "portfolio_update" or request.item_type == "all":
            try:
                portfolio_item = await generate_portfolio_update(user)
                if portfolio_item:
                    feed_items.append(portfolio_item)
            except Exception as e:
                logger.warning(f"Error generating portfolio update: {e}, skipping")
        
        # Psychology tip
        if request.item_type == "psychology" or request.item_type == "all":
            try:
                psychology_item = await generate_psychology_tip()
                feed_items.append(psychology_item)
            except Exception as e:
                logger.warning(f"Error generating psychology tip: {e}, skipping")
        
        # If no items generated, return at least one fallback item
        if not feed_items:
            logger.warning("No feed items generated, returning fallback")
            feed_items.append({
                "title": "Welcome to Your Investment Feed",
                "content": "Your personalized investment feed will appear here. Complete onboarding to get personalized content!",
                "type": "general"
            })
        
        logger.info(f"Returning {len(feed_items)} feed items for user {request.user_id}")
        return {"items": feed_items[:request.limit]}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting feed items: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error loading feed: {str(e)}")

async def generate_market_update() -> Dict:
    """Generate a simple market update in plain English"""
    fallback = {
        "title": "Market Update",
        "content": "Markets move up and down daily - this is normal and expected.",
        "tone": "calm",
        "takeaway": "Focus on your long-term goals, not daily fluctuations."
    }
    
    if not llm_service.is_available():
        return fallback
    
    try:
        # Get current S&P 500 data
        sp500 = yf.Ticker("^GSPC")
        info = sp500.history(period="2d")
        
        if not info.empty:
            current = info['Close'].iloc[-1]
            previous = info['Close'].iloc[-2] if len(info) > 1 else current
            change = current - previous
            change_pct = (change / previous * 100) if previous > 0 else 0
            
            prompt = f"""The S&P 500 is currently at ${current:.2f}, which is {'up' if change >= 0 else 'down'} {abs(change_pct):.2f}% from yesterday.

Create a simple, reassuring one-sentence market update for a beginner investor. Keep it calm and educational. No panic, no jargon.

Format as JSON:
- title: "Market Update"
- content: one sentence explanation
- tone: "calm" or "positive" or "neutral"
- takeaway: one sentence about what this means for long-term investors"""
        else:
            prompt = """Create a simple, reassuring one-sentence market update for a beginner investor. Keep it calm and educational.

Format as JSON:
- title: "Market Update"
- content: one sentence explanation
- tone: "calm" or "positive" or "neutral"
- takeaway: one sentence about what this means for long-term investors"""
        
        try:
            system_prompt = "You are a calm, educational investment assistant. Provide simple market updates."
            content = await call_llm(system_prompt, prompt, temperature=0.7, max_tokens=200)
            
            try:
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()
                
                return json.loads(content)
            except:
                return fallback
        except Exception as api_error:
            # Handle API errors gracefully
            error_str = str(api_error)
            logger.warning(f"LLM API error for market update: {api_error}, using fallback")
            return fallback
    except Exception as e:
        logger.error(f"Error generating market update: {e}")
        return fallback

async def generate_concept_item(user: Dict) -> Dict:
    """Generate an educational concept item based on user's learning progress"""
    if not llm_service.is_available():
        return {
            "title": "This Week's Simple Concept",
            "content": "Learn one new investment concept each week to build your knowledge gradually.",
            "analogy": "Learning to invest is like learning to drive - start slow and build confidence.",
            "type": "concept"
        }
    
    concepts = [
        "compound_interest",
        "diversification",
        "dollar_cost_averaging",
        "risk_and_return",
        "time_horizon"
    ]
    
    completed = user.get('completed_modules', [])
    # Pick a concept user hasn't learned yet
    available_concepts = [c for c in concepts if c not in completed]
    concept = available_concepts[0] if available_concepts else concepts[0]
    
    prompt = f"""Create a simple, 30-second educational post about: {concept}

Format as JSON:
- title: catchy title
- content: 2-3 sentences explaining the concept simply
- analogy: a simple analogy to help understand
- type: "concept"
"""
    
    try:
        system_prompt = "You are a friendly investment educator."
        content = await call_llm(system_prompt, prompt, temperature=0.7, max_tokens=300)
        
        try:
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            
            return json.loads(content)
        except:
            return {
                "title": "This Week's Simple Concept",
                "content": "Learn one new investment concept each week to build your knowledge gradually.",
                "analogy": "Learning to invest is like learning to drive - start slow and build confidence.",
                "type": "concept"
            }
    except Exception as api_error:
        # Handle API errors gracefully
        logger.warning(f"LLM API error for concept item: {api_error}, using fallback")
        return {
            "title": "This Week's Simple Concept",
            "content": "Learn one new investment concept each week to build your knowledge gradually.",
            "analogy": "Learning to invest is like learning to drive - start slow and build confidence.",
            "type": "concept"
        }

async def generate_mistake_item() -> Dict:
    """Generate a common mistake to avoid"""
    if not llm_service.is_available():
        return {
            "title": "Common Mistake to Avoid",
            "content": "Many beginners try to time the market, buying when prices are high and selling when they're low.",
            "solution": "Instead, invest consistently over time - this is called dollar-cost averaging.",
            "type": "mistake"
        }
    
    prompt = """Create a simple post about a common investment mistake beginners make.

Format as JSON:
- title: "Common Mistake to Avoid"
- content: 2-3 sentences about the mistake
- solution: one sentence about how to avoid it
- type: "mistake"
"""
    
    try:
        system_prompt = "You are a helpful investment educator."
        content = await call_llm(system_prompt, prompt, temperature=0.7, max_tokens=250)
        
        try:
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            
            return json.loads(content)
        except:
            return {
                "title": "Common Mistake to Avoid",
                "content": "Many beginners try to time the market, buying when prices are high and selling when they're low.",
                "solution": "Instead, invest consistently over time - this is called dollar-cost averaging.",
                "type": "mistake"
            }
    except Exception as api_error:
        # Handle API errors gracefully
        logger.warning(f"LLM API error for mistake item: {api_error}, using fallback")
        return {
            "title": "Common Mistake to Avoid",
            "content": "Many beginners try to time the market, buying when prices are high and selling when they're low.",
            "solution": "Instead, invest consistently over time - this is called dollar-cost averaging.",
            "type": "mistake"
        }

async def generate_portfolio_update(user: Dict) -> Optional[Dict]:
    """Generate portfolio update if user has one"""
    # Placeholder - would check user's actual portfolio
    return None

async def generate_psychology_tip() -> Dict:
    """Generate an investor psychology tip"""
    if not llm_service.is_available():
        return {
            "title": "Investor Psychology Tip",
            "content": "Remember: you're investing for 10-15 years, not 10-15 days. Daily market movements are just noise.",
            "type": "psychology"
        }
    
    prompt = """Create a simple, reassuring tip about investor psychology.

Format as JSON:
- title: "Investor Psychology Tip"
- content: 2-3 sentences about managing emotions while investing
- type: "psychology"
"""
    
    try:
        system_prompt = "You are a calm, supportive investment coach."
        content = await call_llm(system_prompt, prompt, temperature=0.7, max_tokens=200)
        
        try:
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            
            return json.loads(content)
        except:
            return {
                "title": "Investor Psychology Tip",
                "content": "Remember: you're investing for 10-15 years, not 10-15 days. Daily market movements are just noise.",
                "type": "psychology"
            }
    except Exception as api_error:
        # Handle API errors gracefully
        logger.warning(f"LLM API error for psychology tip: {api_error}, using fallback")
        return {
            "title": "Investor Psychology Tip",
            "content": "Remember: you're investing for 10-15 years, not 10-15 days. Daily market movements are just noise.",
            "type": "psychology"
        }

# Chat endpoint for Q&A
@app.post("/chat")
async def chat(message: ChatMessage):
    """Handle investment education chat questions"""
    if not llm_service.is_available():
        return {
            "response": "I'm sorry, the chat service is not available right now. Please set GEMINI_API_KEY or OPENROUTER_API_KEY in your .env file to enable chat features. Get Gemini key from: https://makersuite.google.com/app/apikey or OpenRouter key from: https://openrouter.ai/keys"
        }
    
    try:
        # Ensure user exists (auto-create in sandbox mode if needed)
        user = await ensure_user_exists(message.user_id)
        
        user_context = ""
        
        if user:
            goals = user.get('investment_goals', [])
            risk_level = user.get('risk_comfort_level', 3)
            experience = user.get('prior_experience', 1)
            user_context = f"User goals: {', '.join(goals) if goals else 'general investing'}. Risk comfort: {risk_level}/5. Experience: {experience}/3."
        
        system_prompt = """You are a friendly, patient investment education assistant. Your goal is to help beginners understand investing in simple, clear terms.

Guidelines:
- Use simple language, no jargon
- Use analogies when helpful
- Be encouraging and supportive
- If asked about specific investments, provide educational information only (not financial advice)
- Keep responses concise (2-3 paragraphs max)
- Always emphasize long-term thinking and risk management"""

        user_prompt = f"{user_context}\n\nUser question: {message.message}"
        
        ai_response = await call_llm(system_prompt, user_prompt, temperature=0.7, max_tokens=500)
        
        # Save interaction
        await db_service.add_interaction(message.user_id, {
            "type": "chat",
            "message": message.message,
            "response": ai_response
        })
        
        return {"response": ai_response}
        
    except Exception as e:
        logger.error(f"Chat error: {e}")
        import traceback
        logger.error(f"Chat error traceback: {traceback.format_exc()}")
        error_msg = "I'm sorry, I encountered an error. Please try rephrasing your question."
        if "API" in str(e) or "authentication" in str(e).lower():
            error_msg = "Chat service is not available. Please configure GEMINI_API_KEY or OPENROUTER_API_KEY."
        return {"response": error_msg}

# User profile endpoints
@app.get("/user/{user_id}")
async def get_user_profile(user_id: str):
    """Get user profile"""
    try:
        # Ensure user exists (auto-create in sandbox mode if needed)
        user = await ensure_user_exists(user_id)
        return user
    except Exception as e:
        logger.error(f"Error getting user profile: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/user/{user_id}/progress")
async def get_user_progress(user_id: str):
    """Get user's learning progress and stats"""
    try:
        # Ensure user exists (auto-create in sandbox mode if needed)
        user = await ensure_user_exists(user_id)
        
        return {
            "completed_modules": len(user.get('completed_modules', [])),
            "learning_streak": user.get('learning_streak', 0),
            "badges_earned": user.get('badges_earned', []),
            "last_learning_date": user.get('last_learning_date')
        }
    except Exception as e:
        logger.error(f"Error getting user progress: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

