"""
Unified LLM Service - Supports both OpenRouter and Gemini API
"""
import os
import json
import logging
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

# Conditional imports - only import if available
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    logger.warning("OpenAI library not available - OpenRouter features will not work")

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    logger.warning("Google Generative AI library not available - Gemini features will not work")


class LLMService:
    """Unified LLM service that supports both OpenRouter and Gemini"""
    
    def __init__(self):
        self.provider = None  # 'openrouter' or 'gemini'
        self.client = None
        self.model_name = None
        self._initialize()
    
    def _initialize(self):
        """Initialize LLM service with available provider"""
        # Check for Gemini first (if explicitly enabled)
        use_gemini = os.getenv("USE_GEMINI", "false").lower() == "true"
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        
        # Check for OpenRouter
        openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        
        if use_gemini and gemini_key:
            if not GEMINI_AVAILABLE:
                logger.warning("Gemini requested but google-generativeai library not installed. Install with: pip install google-generativeai")
            else:
                try:
                    genai.configure(api_key=gemini_key)
                    self.client = genai.GenerativeModel('gemini-1.5-flash')
                    self.provider = 'gemini'
                    self.model_name = 'gemini-1.5-flash'
                    logger.info("✅ Gemini API initialized successfully")
                    logger.info(f"   Using model: {self.model_name}")
                    return
                except Exception as e:
                    logger.warning(f"Failed to initialize Gemini: {e}")
        
        if openrouter_key:
            if not OPENAI_AVAILABLE:
                logger.warning("OpenRouter requested but openai library not installed. Install with: pip install openai")
            else:
                try:
                    self.client = OpenAI(
                        api_key=openrouter_key,
                        base_url="https://openrouter.ai/api/v1"
                    )
                    self.provider = 'openrouter'
                    self.model_name = 'openai/gpt-4o-mini'
                    logger.info("✅ OpenRouter API initialized successfully")
                    logger.info(f"   Using model: {self.model_name}")
                    return
                except Exception as e:
                    logger.warning(f"Failed to initialize OpenRouter: {e}")
        
        # Try Gemini as fallback if available
        if gemini_key and not use_gemini:
            if GEMINI_AVAILABLE:
                try:
                    genai.configure(api_key=gemini_key)
                    self.client = genai.GenerativeModel('gemini-1.5-flash')
                    self.provider = 'gemini'
                    self.model_name = 'gemini-1.5-flash'
                    logger.info("✅ Gemini API initialized as fallback")
                    logger.info(f"   Using model: {self.model_name}")
                    return
                except Exception as e:
                    logger.warning(f"Failed to initialize Gemini fallback: {e}")
        
        logger.warning("⚠️  No LLM provider available")
        logger.warning("   Set GEMINI_API_KEY or OPENROUTER_API_KEY in .env file")
        logger.warning("   Get Gemini key: https://makersuite.google.com/app/apikey")
        logger.warning("   Get OpenRouter key: https://openrouter.ai/keys")
    
    def is_available(self) -> bool:
        """Check if LLM service is available"""
        return self.client is not None and self.provider is not None
    
    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None
    ) -> str:
        """
        Generate text using the configured LLM provider
        
        Args:
            system_prompt: System/instruction prompt
            user_prompt: User input prompt
            temperature: Sampling temperature (0-1)
            max_tokens: Maximum tokens to generate
            
        Returns:
            Generated text response
        """
        if not self.is_available():
            raise Exception("LLM service is not available. Please configure GEMINI_API_KEY or OPENROUTER_API_KEY.")
        
        if self.provider == 'gemini':
            return await self._generate_gemini(system_prompt, user_prompt, temperature, max_tokens)
        elif self.provider == 'openrouter':
            return await self._generate_openrouter(system_prompt, user_prompt, temperature, max_tokens)
        else:
            raise Exception(f"Unknown LLM provider: {self.provider}")
    
    async def _generate_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: Optional[int]
    ) -> str:
        """Generate text using Gemini API"""
        try:
            # Combine system and user prompts for Gemini
            full_prompt = f"{system_prompt}\n\n{user_prompt}"
            
            # Configure generation parameters
            generation_config = {
                'temperature': temperature,
            }
            if max_tokens:
                generation_config['max_output_tokens'] = max_tokens
            
            # Generate response (run in executor since Gemini SDK is sync)
            import asyncio
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.client.generate_content(
                    full_prompt,
                    generation_config=generation_config
                )
            )
            
            return response.text.strip()
            
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            raise
    
    async def _generate_openrouter(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: Optional[int]
    ) -> str:
        """Generate text using OpenRouter API"""
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=temperature,
                max_tokens=max_tokens or 1000
            )
            
            return response.choices[0].message.content.strip()
            
        except Exception as e:
            logger.error(f"OpenRouter API error: {e}")
            raise
    
    def get_provider_info(self) -> Dict[str, Any]:
        """Get information about the current LLM provider"""
        return {
            "provider": self.provider,
            "model": self.model_name,
            "available": self.is_available()
        }


# Global instance
llm_service = LLMService()

