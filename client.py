# client.py
# Asynchronous client for interacting with the Pollinations API

import base64
import logging
from contextvars import ContextVar
from urllib.parse import quote
import aiohttp
import json
import os
import tempfile
import subprocess
import math
import asyncio
import uuid
import time
from typing import Optional, List, Tuple, Any, Dict
import re

from config import CURRENT_TIER_CONFIG, USAGE_TIER, TTS_CHUNK_MAX_CHARS

logger = logging.getLogger(__name__)

# Chiave portata dal singolo chiamante (BYOP). ContextVar: ogni richiesta ha il
# suo contesto, quindi la chiave di uno non finisce nelle chiamate di un altro.
REQUEST_TOKEN: ContextVar[Optional[str]] = ContextVar("pollinations_request_token", default=None)

# Limite per OpenAI Audio API (24MB per sicurezza)
MAX_AUDIO_SIZE_MB = 24

# Durata segmenti per STT in secondi (10 minuti)
STT_SEGMENT_DURATION_SECONDS = 600

# Modelli di default per gli endpoint dedicati (sovrascrivibili per chiamata)
DEFAULT_TRANSCRIPTION_MODEL = "openai/whisper-large-v3"
DEFAULT_TTS_MODEL = "openai/tts-1"

# Limite approssimativo per il payload (in bytes) - circa 4MB per essere sicuri
MAX_PAYLOAD_SIZE = 4 * 1024 * 1024  

# Nuovo limite di caratteri per il testo TTS
TTS_TEXT_LIMIT = 950  # Limite di sicurezza inferiore a 1000

# Formati audio supportati direttamente dall'API
SUPPORTED_AUDIO_FORMATS = {'mp3', 'mp4', 'mpeg', 'mpga', 'm4a', 'wav', 'webm'}

class PollinationsClient:
    """Asynchronous client for interacting with Pollinations AI API services."""
    
    def __init__(self, image_api_url, text_api_url, referrer=None, timeout=300, api_token=None):
        """
        Initialize the Pollinations API client.
        
        Args:
            image_api_url (str): Base URL for the image generation API
            text_api_url (str): Base URL for the text generation API
            referrer (str): Optional referrer string for API tracking
            timeout (int): Timeout for API requests in seconds
            api_token (str): Optional API token for priority access
        """
        self.image_api_url = image_api_url.rstrip('/')
        self.text_api_url = text_api_url.rstrip('/')
        self.referrer = referrer
        self.timeout = timeout
        self.api_token = api_token
        # Base unica per la superficie OpenAI-compat (/v1/*)
        self.api_base_url = self.text_api_url
        logger.debug(f"PollinationsClient initialized with image_api={image_api_url}, text_api={text_api_url}, token={'Yes' if api_token else 'No'}")
    
    def _effective_token(self) -> Optional[str]:
        """Chiave verso upstream: quella del chiamante se c'e', altrimenti quella del server."""
        return REQUEST_TOKEN.get() or self.api_token

    async def _make_request(self, method, url, params=None, headers=None, json_data=None, stream=False, timeout=None):
        """
        Make a HTTP request with appropriate error handling.
        
        Returns:
            tuple: (status_code, response_data, content_type, error_message)
                  If stream=True, response_data will be the response object
        """
        if params is None:
            params = {}
            
        # Add referrer if available
        if self.referrer:
            params['referrer'] = self.referrer
            
        if headers is None:
            headers = {}
        
        # Add API token to headers if available (Bearer authentication)
        token = self._effective_token()
        if token:
            headers['Authorization'] = f'Bearer {token}'

        # Add API key as query parameter if available (fallback auth method)
        if token:
            params['key'] = token
        
        # Use custom timeout if provided, otherwise use instance timeout
        request_timeout = timeout if timeout is not None else self.timeout
        timeout_config = aiohttp.ClientTimeout(total=request_timeout)
        
        try:
            async with aiohttp.ClientSession(timeout=timeout_config) as session:
                async with getattr(session, method)(url, params=params, headers=headers, json=json_data) as response:
                    status_code = response.status
                    content_type = response.headers.get('Content-Type', '')
                    
                    # For streaming responses
                    if stream and status_code == 200:
                        # Return the response object for streaming
                        # La sessione viaggia agganciata alla response: la chiude
                        # l'handler di streaming in server.py (finally).
                        new_session = aiohttp.ClientSession(timeout=timeout_config)
                        try:
                            new_response = await new_session.request(
                                method.upper(), url, params=params, headers=headers, json=json_data)
                        except Exception:
                            await new_session.close()
                            raise
                        if new_response.status == 200:
                            logger.debug(f"Streaming response initiated for {url}")
                            setattr(new_response, '_owner_session', new_session)
                            return new_response.status, new_response, content_type, None
                        error_text = await new_response.text()
                        new_response.close()
                        await new_session.close()
                        return new_response.status, None, content_type, error_text
                        
                    # For successful responses
                    if status_code == 200:
                        # Binari: immagini, audio, video/MP4, GLB/3D e octet-stream
                        if any(t in content_type for t in ('image', 'audio', 'video', 'octet-stream', 'gltf')):
                            data = await response.read()
                        elif 'application/json' in content_type:
                            data = await response.json()
                        else:
                            data = await response.text()
                            
                        return status_code, data, content_type, None
                    
                    # For error responses
                    else:
                        try:
                            error_data = await response.json()
                            if isinstance(error_data, dict) and 'error' in error_data:
                                error_message = error_data['error'].get('message', str(error_data))
                            else:
                                error_message = str(error_data)
                        except Exception:
                            error_message = await response.text()
                        
                        logger.error(f"Request failed: {status_code}, {error_message[:200]}")
                        return status_code, None, content_type, error_message
                        
        except aiohttp.ClientError as e:
            logger.exception(f"Connection error during request to {url}: {str(e)}")
            return 500, None, None, f"Connection error: {str(e)}"
        except Exception as e:
            logger.exception(f"Unexpected error during request to {url}: {str(e)}")
            return 500, None, None, f"Unexpected error: {str(e)}"

    async def proxy_get(self, path, params=None, timeout=None):
        """GET verso upstream con auth/referrer, timeout dedicabile (video/3D)."""
        url = f"{self.api_base_url}{path}"
        return await self._make_request('get', url, params=params, timeout=timeout)

    async def proxy_json(self, method, path, payload=None, stream=False):
        """Inoltro JSON verso upstream (/v1/*), con auth e referrer gia' applicati."""
        url = f"{self.api_base_url}{path}"
        return await self._make_request(method, url, json_data=payload, stream=stream)

    async def proxy_multipart(self, path, fields, files):
        """Inoltro multipart (upload). files: {campo: (filename, bytes, content_type)}."""
        url = f"{self.api_base_url}{path}"
        form = aiohttp.FormData()
        for key, value in (fields or {}).items():
            if value is not None:
                form.add_field(key, str(value))
        for field, (filename, content, content_type) in (files or {}).items():
            form.add_field(field, content, filename=filename, content_type=content_type)
        headers = {}
        token = self._effective_token()
        if token:
            headers['Authorization'] = f'Bearer {token}'
        timeout_config = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout_config) as session:
                async with session.post(url, data=form, headers=headers) as response:
                    content_type = response.headers.get('Content-Type', '')
                    if response.status == 200:
                        if 'application/json' in content_type:
                            return response.status, await response.json(), content_type, None
                        return response.status, await response.read(), content_type, None
                    return response.status, None, content_type, (await response.text())[:500]
        except aiohttp.ClientError as e:
            logger.exception(f"Connection error during multipart request to {url}: {e}")
            return 500, None, None, f"Connection error: {e}"

    async def _enhance_prompt_for_image(self, original_prompt: str, target_model: str) -> str:
        """
        Enhances an image prompt using a text generation model (unity).
        """
        logger.info(f"Enhancing prompt for model {target_model}: '{original_prompt}'")
        
        # System prompt tailored for different models, as seen in the sample script
        if target_model.lower() in ['flux', 'turbo', 'flux-turbo']:
            system_prompt = """You are an expert at creating optimized prompts for FLUX image generation model. Transform the user's input into a detailed, structured prompt following FLUX best practices.

Structure your enhanced prompt with:
1. Main subject and action - what is the focal point doing
2. Setting and background - detailed environment description  
3. Artistic/photographic style - specify medium (watercolor, oil painting, high-res photography, etc.)
4. Mood and atmosphere - lighting conditions, emotional tone
5. Technical details - camera type, lens, aperture if relevant
6. Color palette - dominant colors and tones
7. Composition - foreground, background, element arrangement
8. Three-dimensional aspects - depth, perspective, layering

Use natural, descriptive language with complete sentences. Balance essential details with creative freedom. Avoid generic descriptions or conflicting details.

Return only the enhanced prompt in natural language."""
        else:
            system_prompt = f"""You are an expert at enhancing image generation prompts specifically for the {target_model} model. 
Create detailed, structured prompts that work best with {target_model}'s capabilities.

Include:
- Detailed subject description with action
- Rich environmental context
- Specific artistic style (be explicit about medium: "watercolor painting", "digital art", "photorealistic", etc.)
- Lighting and atmospheric details
- Three-dimensional spatial descriptions
- Technical rendering aspects
- Color and texture details

Adapt the prompt to {target_model}'s strengths. Use natural, descriptive language. Return only the enhanced prompt."""

        enhancement_request = f"Transform this input into an optimized prompt for {target_model}: {original_prompt}"
        
        payload = {
            "model": "unity",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": enhancement_request}
            ],
            "max_tokens": 350,
            "temperature": 0.75
        }
        
        # Use the existing POST method for text generation
        status_code, response_data, _, error = await self.generate_text_post(payload)
        
        if status_code == 200 and isinstance(response_data, dict):
            try:
                enhanced_prompt = response_data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
                if enhanced_prompt and len(enhanced_prompt) > len(original_prompt) * 0.8:
                    logger.info(f"Prompt successfully enhanced: '{enhanced_prompt}'")
                    return enhanced_prompt
                else:
                    logger.warning("Prompt enhancement failed or produced a short result, using original prompt.")
                    return original_prompt
            except (KeyError, IndexError) as e:
                logger.error(f"Error parsing enhanced prompt response: {e}")
                return original_prompt
        else:
            logger.error(f"Failed to enhance prompt, API returned status {status_code}: {error}")
            return original_prompt

    async def _summarize_prompt_for_image(self, long_prompt: str, target_model: str) -> str:
        """
        Summarizes a long image prompt to fit within URL length limits while preserving key visual elements.
        """
        logger.info(f"Summarizing long prompt for model {target_model}: '{long_prompt[:100]}...'")
        
        # Calculate target length (URL-encoded length should be under 1500 chars to be safe)
        target_char_limit = 2000  # Conservative limit for the summary
        
        system_prompt = f"""You are an expert at condensing image generation prompts while preserving all essential visual elements.

Your task: Transform this long prompt into a concise version that is MAXIMUM {target_char_limit} characters while keeping ALL the important visual details.

GUIDELINES:
1. Preserve the main subject and action
2. Keep essential style details (art medium, photography style, etc.)
3. Maintain lighting and mood descriptions
4. Keep important color and composition details
5. Remove redundant or repetitive descriptions
6. Use more concise language without losing meaning
7. Optimize for {target_model} image generation

CRITICAL: Your response must be {target_char_limit} characters or less. Return ONLY the condensed prompt, nothing else."""

        summarization_request = f"Condense this image prompt to maximum {target_char_limit} characters: {long_prompt}"
        
        payload = {
            "model": "unity",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": summarization_request}
            ],
            "max_tokens": 200,  # Limit tokens to ensure concise response
            "temperature": 0.3   # Lower temperature for more focused summarization
        }
        
        # Use the existing POST method for text generation
        status_code, response_data, _, error = await self.generate_text_post(payload)
        
        if status_code == 200 and isinstance(response_data, dict):
            try:
                summarized_prompt = response_data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
                
                # Validate the summarized prompt meets our length requirements
                if summarized_prompt and len(summarized_prompt) <= target_char_limit:
                    logger.info(f"Prompt successfully summarized from {len(long_prompt)} to {len(summarized_prompt)} characters")
                    logger.info(f"Summarized prompt: '{summarized_prompt}'")
                    return summarized_prompt
                elif summarized_prompt:
                    # If still too long, truncate but preserve end punctuation
                    truncated = summarized_prompt[:target_char_limit-3].rstrip()
                    if not truncated.endswith(('.', '!', '?')):
                        truncated += "..."
                    logger.warning(f"AI summary was still too long ({len(summarized_prompt)} chars), truncated to {len(truncated)} chars")
                    return truncated
                else:
                    logger.warning("AI summarization produced empty result, falling back to intelligent truncation")
                    return self._intelligent_truncate(long_prompt, target_char_limit)
                    
            except (KeyError, IndexError) as e:
                logger.error(f"Error parsing summarized prompt response: {e}")
                return self._intelligent_truncate(long_prompt, target_char_limit)
        else:
            logger.error(f"Failed to summarize prompt, API returned status {status_code}: {error}")
            return self._intelligent_truncate(long_prompt, target_char_limit)

    def _intelligent_truncate(self, prompt: str, max_length: int) -> str:
        """
        Intelligently truncates a prompt by trying to cut at sentence or phrase boundaries.
        """
        if len(prompt) <= max_length:
            return prompt
            
        # Try to cut at sentence boundaries first
        truncated = prompt[:max_length-3]
        
        # Look for sentence endings
        for punct in ['. ', '! ', '? ']:
            if punct in truncated:
                last_sentence = truncated.rfind(punct)
                if last_sentence > max_length * 0.6:  # Don't cut too early
                    return truncated[:last_sentence + 1].strip()
        
        # Look for comma boundaries
        last_comma = truncated.rfind(', ')
        if last_comma > max_length * 0.7:
            return truncated[:last_comma].strip() + "..."
        
        # Look for word boundaries
        last_space = truncated.rfind(' ')
        if last_space > max_length * 0.8:
            return truncated[:last_space].strip() + "..."
        
        # Last resort: hard truncate
        return truncated.strip() + "..."

    async def generate_image(self, prompt, width=None, height=None, seed=None,
               model="tongyi-mai/z-image-turbo", enhance=False, safe=False,
               transparent: bool = False, json: bool = False):
        """
        Generate an image using the Pollinations API, with optional prompt enhancement.
        
        Args:
            prompt (str): Text description of the image
            width (int, optional): Width of the generated image. Default: 1024
            height (int, optional): Height of the generated image. Default: 1024  
            seed (int, optional): Seed for reproducible results. Auto-generated if None
            model (str, optional): Model for generation. Default: "flux"
            enhance (bool, optional): Enhance prompt using LLM. Default: False
            safe (bool, optional): Enable strict NSFW filtering. Default: False
            transparent (bool, optional): [EXPERIMENTAL] Transparent background. Default: False
            json (bool, optional): Return JSON response with base64 image. Default: False
            
        Returns:
            tuple: (status_code, image_data, content_type, error_message)
                  If json=True, image_data is dict with base64 and metadata
                  If json=False, image_data is binary image data
        """
        
        # Log del parametro json ricevuto
        logger.info(f"generate_image called with json={json}, prompt='{prompt[:50]}...', model={model}")
        
        # Validate parameters
        if not prompt or not isinstance(prompt, str):
            raise ValueError("prompt must be a non-empty string")
        if width is not None and (not isinstance(width, int) or width <= 0):
            raise ValueError("width must be a positive integer")
        if height is not None and (not isinstance(height, int) or height <= 0):
            raise ValueError("height must be a positive integer")
        if seed is not None and not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        
        # Generate a random seed if none provided
        if seed is None:
            import random
            seed = random.randint(1, 2147483647)
            logger.info(f"No seed provided, generated random seed: {seed}")
    
        original_prompt = prompt
        final_prompt = prompt
        if enhance:
            final_prompt = await self._enhance_prompt_for_image(prompt, model)

        # Decide GET vs POST based on prompt length
        encoded_prompt = quote(final_prompt)
        estimated_url_length = len(f"{self.image_api_url}/image/{encoded_prompt}") + 200  # +200 for params
    
        if estimated_url_length > 2000:  # URL length limit
            logger.warning(f"Prompt too long for GET ({estimated_url_length} chars). Summarizing prompt to fit URL limits.")
            # Summarize prompt to fit within URL limits using AI
            final_prompt = await self._summarize_prompt_for_image(final_prompt, model)
            
        logger.info(f"Using GET method for prompt ({len(final_prompt)} chars)")
        return await self._generate_image_get(final_prompt, width, height, seed, model, safe, transparent, json, original_prompt, enhance)

    async def _generate_image_get(self, final_prompt, width, height, seed, model, safe, transparent, json, original_prompt, enhance):
        """Original GET method logic"""
        encoded_prompt = quote(final_prompt)
        url = f"{self.image_api_url}/image/{encoded_prompt}"
        params = {}
        
        if width: params['width'] = width
        if height: params['height'] = height
        if seed is not None:
            params['seed'] = seed
        if model: params['model'] = model
        if safe: params['safe'] = 'true'
        # 'transparent' is supported for gptimage in the new API
        if transparent: params['transparent'] = 'true'
        if enhance: params['enhance'] = 'true'
        
        logger.debug(f"Calling Pollinations image API (GET): {url} with params {params}")
        logger.info(f"Final prompt for image generation: '{final_prompt}' with seed: {seed}")
        
        status_code, response_data, content_type, error = await self._make_request('get', url, params=params)
        
        # Log della risposta ricevuta
        logger.info(f"Image API response: status_code={status_code}, content_type={content_type}, json_requested={json}")
        logger.info(f"Response data type: {type(response_data)}, size: {len(response_data) if response_data else 0} bytes")
        
        if json and status_code == 200 and response_data:
            logger.info(f"JSON mode requested and conditions met - converting to JSON response")
            logger.debug(f"Starting base64 conversion for {len(response_data)} bytes of image data")
            
            # Convert binary image data to base64
            import base64
            image_base64 = base64.b64encode(response_data).decode('utf-8')
            logger.debug(f"Base64 conversion completed, length: {len(image_base64)} chars")
            
            # Determine image format from content type
            image_format = "png"
            if content_type:
                if "jpeg" in content_type or "jpg" in content_type:
                    image_format = "jpeg"
                elif "webp" in content_type:
                    image_format = "webp"
            logger.debug(f"Detected image format: {image_format} from content_type: {content_type}")
            
            # Build comprehensive JSON response
            json_response = {
                "success": True,
                "image": {
                    "data": image_base64,
                    "format": image_format,
                    "size_bytes": len(response_data)
                },
                "generation_parameters": {
                    "original_prompt": original_prompt,
                    "final_prompt": final_prompt,
                    "prompt_enhanced": enhance and original_prompt != final_prompt,
                    "model": model,
                    "width": width,
                    "height": height,
                    "seed": seed,
                    "safe": safe,
                    "transparent": transparent
                },
                "metadata": {
                    "generation_time": None,  # Could be tracked if needed
                    "api_endpoint": url,
                    "content_type": content_type
                }
            }
            
            logger.info(f"JSON response created successfully - image format: {image_format}, size: {len(response_data)} bytes")
            logger.info(f"JSON response contains: success={json_response['success']}, prompt_enhanced={json_response['generation_parameters']['prompt_enhanced']}")
            logger.debug(f"JSON response structure: keys={list(json_response.keys())}, content_type_return=application/json")
            
            return status_code, json_response, "application/json", error
        
        # Log perché non entra nella modalità JSON
        elif json:
            logger.warning(f"JSON mode requested but conditions not met - status_code={status_code}, has_data={bool(response_data)}")
            if status_code != 200:
                logger.warning(f"Status code is not 200: {status_code}, error: {error}")
            if not response_data:
                logger.warning(f"No response data received from API")
        
        # Return original binary response if json=False or if there was an error
        logger.info(f"Returning binary response - json={json}, status_code={status_code}, has_data={bool(response_data)}")
        return status_code, response_data, content_type, error

    async def list_image_models(self):
        """Get a list of available image generation models."""
        url = f"{self.image_api_url}/image/models"
        
        logger.debug(f"Retrieving available image models from: {url}")
        
        status_code, models_data, content_type, error = await self._make_request('get', url)
        return status_code, models_data, error

    async def list_text_models(self):
        """Get a list of available text generation models and voices."""
        url = f"{self.text_api_url}/text/models"
        
        logger.debug(f"Retrieving available text models from: {url}")
        
        status_code, models_data, content_type, error = await self._make_request('get', url)
        return status_code, models_data, error

    async def list_all_models(self):
        """Get the unified model catalog (every category) in one request."""
        url = f"{self.text_api_url}/models"

        logger.debug(f"Retrieving unified model catalog from: {url}")

        status_code, models_data, content_type, error = await self._make_request('get', url)
        return status_code, models_data, error

    async def get_account_balance(self):
        """Get current pollen balance for the configured API key."""
        url = f"{self.text_api_url}/account/balance"
        
        logger.debug(f"Retrieving account balance from: {url}")
        
        status_code, response_data, content_type, error = await self._make_request('get', url)
        return status_code, response_data, error

    async def generate_text_get(self, prompt, model="openai/gpt-5.4-nano", seed=None, json=False,
                          system=None, stream=False):
        """Generate text using GET request method."""
        encoded_prompt = quote(prompt)
        url = f"{self.text_api_url}/text/{encoded_prompt}"
        params = {}
        
        if model: params['model'] = model
        if seed is not None: params['seed'] = seed
        if json: params['json'] = 'true'
        if system: params['system'] = system
        if stream: params['stream'] = 'true'
        
        logger.debug(f"Calling Pollinations text API (GET): {url} with params {params}")
        
        return await self._make_request('get', url, params=params, stream=stream)

    async def generate_text_post(self, payload, stream=False):
        """Generate text using POST request method (OpenAI compatible endpoint)."""
        url = f"{self.text_api_url}/v1/chat/completions"
        headers = {'Content-Type': 'application/json'}
        
        logger.debug(f"Calling Pollinations text API (POST): {url}")
        
        return await self._make_request('post', url, headers=headers, json_data=payload, stream=stream)

    def _prepare_tts_prompt(self, text: str, language: Optional[str] = None) -> str:
        """Restituisce il testo da leggere, e nient'altro.

        Prima qui si costruiva un paragrafo di istruzioni con il testo fra virgolette:
        il modello leggeva ad alta voce anche le istruzioni. Verificato con whisper:
        la trascrizione dell'audio cominciava con "Certo, ora ripetero' il testo...".
        Il TTS deve ricevere solo il testo; il tono lo decide la voce, non un prompt.
        """
        return text.strip()

    async def generate_audio_tts_get(self, prompt: str, voice: str = "alloy", language: Optional[str] = None, model: str = "openai-audio", instructions: Optional[str] = None) -> Tuple[int, Optional[bytes], Optional[str], Optional[str]]:
        """Generate speech audio from text using GET method. Internally uses the POST audio-capable endpoint."""

        # Use tier-aware character limit (optionally overridden via config)
        max_chars = CURRENT_TIER_CONFIG['max_chars_per_request']
        if TTS_CHUNK_MAX_CHARS and TTS_CHUNK_MAX_CHARS > 0:
            max_chars = min(max_chars, TTS_CHUNK_MAX_CHARS)

        if len(prompt) > max_chars:
            logger.info(f"Text is too long for GET method based on '{USAGE_TIER.upper()}' tier limits ({len(prompt)} > {max_chars}), switching to POST chunking.")
            return await self.generate_audio_tts_post(prompt, voice, model=model, language=language, instructions=instructions)

        # Use the same POST pipeline for short text to align with the new API
        return await self._generate_single_chunk_audio(prompt, voice, model, "mp3", False, language, instructions=instructions)

    async def _enhance_text_for_tts(self, original_text: str, language: Optional[str]) -> str:
        """
        Enhances text for TTS by correcting grammar and improving flow using an LLM.
        """
        logger.info(f"Enhancing text for TTS quality: '{original_text[:100]}...'")
        
        language_clause = f"The text is in {language}." if language else "The language of the text should be auto-detected."

        system_prompt = f"""You are an expert in text pre-processing for Text-to-Speech (TTS) synthesis. Your task is to take the user's text and refine it for the most natural and clear audio output possible.

Follow these rules:
1.  **Correct Grammar and Typos:** Fix any spelling mistakes, grammatical errors, and punctuation issues.
2.  **Improve Flow:** Rephrase awkward sentences to make them sound more natural when spoken. Do not change the core meaning.
3.  **Expand Abbreviations:** Write out common abbreviations (e.g., "Dr." becomes "Doctor", "St." becomes "Saint" or "Street" based on context).
4.  **Handle Numbers:** Ensure numbers are written in a way that is easy to read aloud.
5.  **Preserve Intent:** The original meaning and intent of the text must be fully preserved.
6.  **Language:** {language_clause}

Return ONLY the improved text, with no extra explanations or commentary.
"""
        
        payload = {
            "model": "unity",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": original_text}
            ],
            "max_tokens": int(len(original_text) * 1.5),  # Allow for some expansion
            "temperature": 0.2  # Low temperature for factual correction
        }
        
        status_code, response_data, _, error = await self.generate_text_post(payload)
        
        if status_code == 200 and isinstance(response_data, dict):
            try:
                enhanced_text = response_data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
                if enhanced_text:
                    logger.info(f"Text successfully enhanced. Length change: {len(original_text)} -> {len(enhanced_text)}")
                    logger.debug(f"Enhanced text: '{enhanced_text[:100]}...'")
                    return enhanced_text
                else:
                    logger.warning("Text enhancement resulted in an empty string. Falling back to original text.")
                    return original_text
            except (KeyError, IndexError) as e:
                logger.error(f"Error parsing enhanced text response: {e}. Falling back to original text.")
                return original_text
        else:
            logger.error(f"Failed to enhance text (Status: {status_code}, Error: {error}). Falling back to original text.")
            return original_text

    async def _rewrite_text_for_policy(self, problematic_text: str, language: Optional[str]) -> str:
        """
        Rewrites text to make it compliant with content policies while preserving meaning.
        """
        logger.info(f"Rewriting text for policy compliance: '{problematic_text[:100]}...'")
        
        language_clause = f"The text is in {language}." if language else "The language of the text should be auto-detected."

        system_prompt = f"""You are an expert text editor specialized in making content compliant with OpenAI's usage policies while preserving the original meaning and intent.

Your task: Rewrite the provided text to ensure it meets content policy requirements while keeping the core message intact.

GUIDELINES:
1. **Preserve Core Meaning**: The rewritten text must convey the same essential information and intent.
2. **Remove Policy Violations**: Identify and rephrase any content that might trigger content filters (violence, hate speech, explicit content, etc.).
3. **Use Neutral Language**: Replace potentially problematic words with neutral, appropriate alternatives.
4. **Maintain Readability**: The result should be natural and readable when spoken aloud.
5. **Keep Context**: Don't remove important context, just make it policy-compliant.
6. **Language**: {language_clause}

IMPORTANT: Return ONLY the rewritten text, with no explanations, comments, or additional text.
"""
        
        payload = {
            "model": "unity",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": problematic_text}
            ],
            "max_tokens": int(len(problematic_text) * 1.2),  # Allow for slight expansion
            "temperature": 0.3  # Low temperature for consistent, policy-compliant results
        }
        
        status_code, response_data, _, error = await self.generate_text_post(payload)
        
        if status_code == 200 and isinstance(response_data, dict):
            try:
                rewritten_text = response_data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
                if rewritten_text:
                    logger.info(f"Text successfully rewritten for policy compliance. Length change: {len(problematic_text)} -> {len(rewritten_text)}")
                    logger.debug(f"Rewritten text: '{rewritten_text[:100]}...'")
                    return rewritten_text
                else:
                    logger.warning("Text rewriting resulted in an empty string. Falling back to original.")
                    return problematic_text
            except (KeyError, IndexError) as e:
                logger.error(f"Error parsing rewritten text response: {e}. Falling back to original.")
                return problematic_text
        else:
            logger.error(f"Failed to rewrite text for policy compliance (Status: {status_code}, Error: {error}). Falling back to original.")
            return problematic_text

    def _is_content_filter_error(self, status_code: int, error_message: str) -> bool:
        """
        Determines if an error is related to content filtering.
        """
        if status_code != 400:
            return False
        
        # Common content filter error indicators
        filter_indicators = [
            "content", "policy", "violation", "inappropriate", "filtered", 
            "safety", "harmful", "offensive", "blocked", "restricted"
        ]
        
        error_lower = str(error_message).lower()
        return any(indicator in error_lower for indicator in filter_indicators)

    async def generate_audio_tts_post(self, text: str, voice: str, model: str = "openai-audio", response_format: str = "mp3", private: bool = False, language: Optional[str] = None, enhance_text: bool = False, instructions: Optional[str] = None) -> Tuple[int, Optional[bytes], Optional[str], Optional[str]]:
        """
        Generate speech audio from text using POST method.
        Handles long text by splitting it into chunks and merging the audio.
        Optionally enhances the text for better TTS quality.
        """
        
        # Optionally enhance the text before processing
        if enhance_text:
            text = await self._enhance_text_for_tts(text, language)

        # Use tier-aware character limit (optionally overridden via config)
        max_chars = CURRENT_TIER_CONFIG['max_chars_per_request']
        if TTS_CHUNK_MAX_CHARS and TTS_CHUNK_MAX_CHARS > 0:
            max_chars = min(max_chars, TTS_CHUNK_MAX_CHARS)

        if len(text) <= max_chars:
            # If the text is short, use the single chunk logic
            return await self._generate_single_chunk_audio(text, voice, model, response_format, private, language, instructions=instructions)
        else:
            # If the text is long, use the new handler
            logger.info(f"Text is long ({len(text)} chars), starting chunking process based on '{USAGE_TIER.upper()}' tier limits.")
            return await self._handle_long_text_tts(text, voice, model, response_format, private, language, instructions=instructions)

    def _split_text(self, text: str, max_chars: int) -> List[str]:
        """
        Splits text into chunks smaller than max_chars, trying to split at sentence endings.
        """
        chunks = []
        text = text.strip()
        while len(text) > max_chars:
            split_pos = -1
            # Prioritize sentence endings
            possible_splits = [m.start() for m in re.finditer(r'[.!?]\s', text[:max_chars])]
            if possible_splits:
                split_pos = max(possible_splits)
            
            if split_pos == -1:
                # Fallback to commas or spaces
                split_pos = text.rfind(',', 0, max_chars)
            if split_pos == -1:
                split_pos = text.rfind(' ', 0, max_chars)
            if split_pos == -1:
                # Force split if no natural break found
                split_pos = max_chars - 1

            chunks.append(text[:split_pos+1].strip())
            text = text[split_pos+1:].strip()
        
        if text:
            chunks.append(text)

        logger.info(f"Split text into {len(chunks)} chunks.")
        return chunks

    async def _handle_long_text_tts(self, text: str, voice: str, model: str, response_format: str, private: bool, language: Optional[str], instructions: Optional[str] = None) -> Tuple[int, Optional[bytes], Optional[str], Optional[str]]:
        """Orchestrates the process of handling long text for TTS, including tier-aware rate limiting."""
        
        # Use tier-aware character limit for splitting (optionally overridden via config)
        max_chars = CURRENT_TIER_CONFIG['max_chars_per_request']
        if TTS_CHUNK_MAX_CHARS and TTS_CHUNK_MAX_CHARS > 0:
            max_chars = min(max_chars, TTS_CHUNK_MAX_CHARS)
        text_chunks = self._split_text(text, max_chars)
        
        audio_segments_paths = []
        temp_dir = tempfile.mkdtemp(prefix="tts_chunks_")
        merged_audio_path = None
        
        try:
            tasks = []
            # Get the delay from the tier configuration
            delay_per_request = CURRENT_TIER_CONFIG['delay_between_requests']
            logger.info(f"Staggering {len(text_chunks)} TTS requests with a {delay_per_request}s delay between each start.")

            for i, chunk in enumerate(text_chunks):
                # Calculate the delay for this specific task
                task_delay = i * delay_per_request
                
                # Create a task for each chunk generation, passing the calculated delay
                task = self._generate_single_chunk_audio(
                    chunk, voice, model, "wav", private, language, i, len(text_chunks),
                    delay=task_delay, instructions=instructions
                )
                tasks.append(task)
            
            # Run all chunk generation tasks concurrently (they will self-delay)
            results = await asyncio.gather(*tasks, return_exceptions=False)

            # Process results and save successful audio segments
            for i, result in enumerate(results):
                status, audio_data, _, _ = result
                if status == 200 and audio_data:
                    segment_path = os.path.join(temp_dir, f"segment_{i}.wav")
                    with open(segment_path, 'wb') as f:
                        f.write(audio_data)
                    audio_segments_paths.append(segment_path)
                else:
                    logger.error(f"Failed to generate audio for chunk {i+1}. Aborting.")
                    return 500, None, None, f"Failed on chunk {i+1}"

            if not audio_segments_paths:
                return 500, None, None, "No audio segments were generated."

            logger.info("All chunks converted. Merging audio segments...")
            merged_audio_path = await self._merge_audio_files(audio_segments_paths, temp_dir, response_format)

            if merged_audio_path:
                with open(merged_audio_path, 'rb') as f:
                    final_audio_data = f.read()
                media_type = 'audio/mpeg' if response_format == 'mp3' else 'audio/wav'
                return 200, final_audio_data, media_type, None
            else:
                return 500, None, None, "Failed to merge audio segments."

        finally:
            # Comprehensive cleanup of the temporary directory
            if os.path.exists(temp_dir):
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.debug(f"Cleaned up temporary directory: {temp_dir}")

    async def _merge_audio_files(self, file_paths: List[str], temp_dir: str, output_format: str) -> Optional[str]:
        """Merges multiple audio files into one using ffmpeg."""
        list_file_path = os.path.join(temp_dir, "merge_list.txt")
        output_path = os.path.join(temp_dir, f"merged_output.{output_format}")

        try:
            with open(list_file_path, 'w', encoding='utf-8') as f:
                for path in file_paths:
                    # Correctly format path for ffmpeg's concat demuxer
                    path_for_ffmpeg = os.path.abspath(path).replace(os.sep, '/')
                    f.write(f"file '{path_for_ffmpeg}'\n")

            cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', list_file_path, '-y']
            if output_format == 'mp3':
                cmd.extend(['-c:a', 'libmp3lame', '-q:a', '4'])
            else:
                # If WAV or other, just copy the codec
                cmd.extend(['-c', 'copy'])
            cmd.append(output_path)
            
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                logger.info(f"Successfully merged {len(file_paths)} files into {output_path}")
                return output_path
            else:
                logger.error(f"FFmpeg merge failed: {stderr.decode()}")
                return None
        except Exception as e:
            logger.error(f"Error during audio merge: {e}")
            return None

    async def _generate_single_chunk_audio(self, text: str, voice: str, model: str, response_format: str, private: bool, language: Optional[str], chunk_num: int = 0, total_chunks: int = 1, delay: float = 0.0, instructions: Optional[str] = None) -> Tuple[int, Optional[bytes], Optional[str], Optional[str]]:
        """Generate speech audio for a single chunk of text using POST method, with an initial delay and content filter fallback."""
        
        # Apply the calculated delay for rate-limiting before making the request
        if delay > 0:
            logger.debug(f"Delaying chunk {chunk_num + 1} by {delay:.2f} seconds for rate limiting.")
            await asyncio.sleep(delay)

        if total_chunks > 1:
            logger.info(f"Processing chunk {chunk_num + 1}/{total_chunks}...")

        # Prepare for potential content filter fallback
        current_text = text
        attempt_count = 0
        max_attempts = 2
        last_status_code = 500
        last_content_type = None
        last_error = "Unknown error"

        while attempt_count < max_attempts:
            attempt_count += 1
            
            # Log attempt information
            if attempt_count > 1:
                logger.info(f"Content filter fallback attempt {attempt_count} for chunk {chunk_num + 1}")

            status_code, audio_data, content_type, error = await self._make_tts_api_call(
                current_text, voice, model, response_format, private, language, chunk_num, total_chunks,
                instructions=instructions,
            )
            
            # Store the last results for potential error reporting
            last_status_code = status_code
            last_content_type = content_type
            last_error = error or "Unknown error"

            # Check if the request was successful
            if status_code == 200 and audio_data:
                if attempt_count > 1:
                    logger.info(f"Content filter fallback successful for chunk {chunk_num + 1}")
                return status_code, audio_data, content_type, error

            # Check if this is a content filter error and we can try fallback
            if self._is_content_filter_error(status_code, error or "") and attempt_count < max_attempts:
                logger.warning(f"Content filter detected for chunk {chunk_num + 1}. Attempting to rewrite text...")
                
                # Try to rewrite the text to be policy-compliant
                rewritten_text = await self._rewrite_text_for_policy(current_text, language)
                
                if rewritten_text != current_text:
                    logger.info(f"Text rewritten for chunk {chunk_num + 1}, retrying TTS...")
                    current_text = rewritten_text
                    continue
                else:
                    logger.warning(f"Text rewriting didn't change content for chunk {chunk_num + 1}. Original error will be returned.")
                    break
            else:
                # Non-content-filter error or max attempts reached
                break

        # If we get here, all attempts failed
        logger.error(f"All TTS attempts failed for chunk {chunk_num + 1}: {last_status_code} - {last_error}")
        return last_status_code, None, last_content_type, last_error

    async def _make_tts_api_call(self, text: str, voice: str, model: str, response_format: str, private: bool, language: Optional[str], chunk_num: int = 0, total_chunks: int = 1, instructions: Optional[str] = None) -> Tuple[int, Optional[bytes], Optional[str], Optional[str]]:
        """Una chiamata TTS sull'endpoint dedicato, che ritorna i byte audio.

        Prima passava da /v1/chat/completions con modalities=["text","audio"]: il
        modello leggeva anche il preambolo di istruzioni e, tolto quello, rispondeva
        conversando invece di leggere. /v1/audio/speech legge il testo e basta.
        """
        url = f"{self.text_api_url}/v1/audio/speech"
        headers = {'Content-Type': 'application/json'}

        tts_model = model if model and model != 'openai-audio' else DEFAULT_TTS_MODEL
        payload = {
            "model": tts_model,
            "input": self._prepare_tts_prompt(text, language),
            "voice": voice or "alloy",
            "response_format": response_format or "mp3",
        }
        # Istruzioni di stile: le onorano i modelli che le supportano (Gemini/Qwen TTS).
        # Per gli altri (openai/tts-1, elevenlabs) il campo viene ignorato.
        if instructions:
            payload["instructions"] = instructions

        wanted_format = (response_format or "mp3").lower()
        upstream_format = wanted_format
        status_code, response_data, content_type, error = await self._make_request(
            'post', url, headers=headers, json_data=payload, timeout=180)

        # Alcuni modelli (es. google/gemini-*-tts) producono solo wav/pcm: si riprova
        # in wav e si converte in mp3 con ffmpeg, cosi' il chiamante vede sempre mp3.
        if (status_code != 200 and upstream_format == "mp3"
                and "response_format" in str(error or "").lower()):
            logger.info(f"TTS chunk {chunk_num+1}: {tts_model} rifiuta mp3, riprovo in wav.")
            payload["response_format"] = upstream_format = "wav"
            status_code, response_data, content_type, error = await self._make_request(
                'post', url, headers=headers, json_data=payload, timeout=180)

        logger.debug(f"TTS chunk {chunk_num+1}: status={status_code}, content_type={content_type}")

        if status_code != 200:
            logger.warning(f"TTS chunk {chunk_num+1} failed: {status_code} - {error}")
            return status_code, None, content_type, error

        if isinstance(response_data, bytes) and response_data:
            if upstream_format != wanted_format and wanted_format == "mp3":
                converted_path = await self._convert_audio_data(response_data, upstream_format, "mp3")
                if converted_path:
                    with open(converted_path, 'rb') as fh:
                        response_data = fh.read()
                    content_type = "audio/mpeg"
                    logger.info(f"TTS chunk {chunk_num+1}: convertito {upstream_format} -> mp3.")
                else:
                    logger.warning(f"TTS chunk {chunk_num+1}: conversione in mp3 fallita, "
                                   f"restituisco {upstream_format}.")
            return 200, response_data, content_type, None

        logger.error(f"TTS chunk {chunk_num+1}: nessun byte audio (content_type={content_type})")
        return 502, None, content_type, "TTS upstream returned no audio"

    async def _convert_audio_data(self, audio_data: bytes, input_format: str, output_format: str) -> Optional[str]:
        """Converts binary audio data from one format to another using ffmpeg."""
        input_file = None
        output_file = None
        process = None
        try:
            # Create a temporary file for the input audio data
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{input_format}") as temp_in:
                temp_in.write(audio_data)
                input_file = temp_in.name

            # Create a temporary file for the output audio
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{output_format}") as temp_out:
                output_file = temp_out.name

            cmd = ['ffmpeg', '-i', input_file, '-y'] # -y to overwrite output file
            if output_format == 'mp3':
                cmd.extend(['-c:a', 'libmp3lame', '-q:a', '4']) # VBR quality
            
            cmd.append(output_file)
            
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                return output_file
            else:
                logger.error(f"FFmpeg conversion failed: {stderr.decode()}")
                return None
        except Exception as e:
            logger.error(f"Error during audio data conversion: {e}")
            return None
        finally:
            # Cleanup temporary files
            if input_file and os.path.exists(input_file):
                os.remove(input_file)
            # If conversion failed, cleanup the output file as well
            if output_file and process and process.returncode != 0 and os.path.exists(output_file):
                 os.remove(output_file)

    async def _run_ffmpeg_command(self, command: List[str]) -> Tuple[bool, str, str]:
        """Runs an FFmpeg command asynchronously and captures output."""
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                return True, stdout.decode(), stderr.decode()
            else:
                logger.error(f"FFmpeg command failed with code {process.returncode}: {' '.join(command)}")
                logger.error(f"FFmpeg stderr: {stderr.decode()}")
                return False, stdout.decode(), stderr.decode()
        except FileNotFoundError:
            logger.error("ffmpeg not found. Please ensure ffmpeg is installed and in the system's PATH.")
            return False, "", "ffmpeg not found"
        except Exception as e:
            logger.error(f"Exception running ffmpeg command: {e}")
            return False, "", str(e)

    async def _preprocess_audio(self, input_path: str) -> Optional[str]:
        """
        Pre-processes an audio file using ffmpeg to optimize it for STT.
        Converts to mono, 16kHz MP3.
        """
        logger.info(f"Preprocessing audio file: {input_path}")
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
            output_path = tmp_file.name
        
        command = [
            "ffmpeg",
            "-i", input_path,
            "-f", "mp3",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "64k",
            "-filter:a", "highpass=f=80,lowpass=f=8000",
            "-y",
            output_path
        ]
        
        success, _, stderr = await self._run_ffmpeg_command(command)
        
        if success and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            logger.info(f"Audio preprocessed successfully to {output_path}")
            return output_path
        else:
            logger.error(f"Failed to preprocess audio file {input_path}. FFmpeg stderr: {stderr}")
            if os.path.exists(output_path):
                os.remove(output_path)
            return None

    async def _get_audio_duration(self, audio_path: str) -> Optional[float]:
        """Gets the duration of an audio file in seconds using ffprobe."""
        command = [
            "ffprobe",
            "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            audio_path
        ]
        success, stdout, stderr = await self._run_ffmpeg_command(command)
        if success:
            try:
                return float(stdout.strip())
            except (ValueError, TypeError):
                logger.error(f"Could not parse ffprobe duration output: {stdout}")
                return None
        logger.error(f"Failed to get audio duration for {audio_path}. Stderr: {stderr}")
        return None

    async def _split_audio(self, input_path: str, segment_duration: int) -> List[str]:
        """Splits an audio file into smaller segments using ffmpeg."""
        total_duration = await self._get_audio_duration(input_path)
        if not total_duration:
            return []

        num_segments = math.ceil(total_duration / segment_duration)
        logger.info(f"Audio duration is {total_duration:.2f}s. Splitting into {num_segments} segments.")
        
        segment_paths = []
        temp_dir = tempfile.mkdtemp(prefix="stt_segments_")

        for i in range(num_segments):
            start_time = i * segment_duration
            segment_path = os.path.join(temp_dir, f"segment_{i+1}.mp3")
            
            command = [
                "ffmpeg",
                "-i", input_path,
                "-ss", str(start_time),
                "-t", str(segment_duration),
                "-ac", "1",
                "-ar", "16000",
                "-b:a", "64k",
                "-y",
                segment_path
            ]
            
            success, _, _ = await self._run_ffmpeg_command(command)
            if success and os.path.exists(segment_path):
                segment_paths.append(segment_path)
            else:
                logger.error(f"Failed to create segment {i+1} for {input_path}")
        
        return segment_paths

    async def _transcribe_segment_modern(self, audio_path, prompt, language, model):
        """Trascrizione con l'endpoint dedicato /v1/audio/transcriptions (multipart).

        Il vecchio giro passava da /v1/chat/completions con input_audio base64: il
        modello risponde 200 con content null e la trascrizione si perde.
        """
        model = model or DEFAULT_TRANSCRIPTION_MODEL
        if model == "openai-audio":
            model = DEFAULT_TRANSCRIPTION_MODEL
        try:
            with open(audio_path, 'rb') as fh:
                audio_data = fh.read()
        except OSError as e:
            logger.error(f"Cannot read audio segment {audio_path}: {e}")
            return None
        files = {"file": (os.path.basename(audio_path), audio_data, "audio/mpeg")}
        fields = {"model": model, "response_format": "json"}
        if language:
            fields["language"] = language
        if prompt:
            fields["prompt"] = prompt[:900]
        status, data, _ctype, error = await self.proxy_multipart("/v1/audio/transcriptions", fields, files)
        if status != 200 or not isinstance(data, dict):
            logger.error(f"Transcription failed for {os.path.basename(audio_path)}: {status} - {error}")
            return None
        text = (data.get("text") or data.get("transcription") or "").strip()
        if not text:
            logger.error(f"Transcription empty for {os.path.basename(audio_path)}: {str(data)[:200]}")
            return None
        logger.info(f"Transcribed {os.path.basename(audio_path)}: {len(text)} chars")
        return text

    async def transcribe_audio_file_stt(self, audio_file_path: str, original_filename: str, prompt: str = "Trascrivi questo audio.", language: Optional[str] = None, private: bool = False, model: str = "openai/whisper-large-v3", diarize: bool = False) -> Tuple[int, Optional[Dict[str, Any]], Optional[str]]:
        """
        Trascrive un file audio usando una pipeline avanzata:
        1. Pre-elabora l'audio con FFmpeg per ottimizzarlo.
        2. Se il file è troppo grande, lo divide in segmenti.
        3. Trascrive i segmenti in parallelo.
        4. Combina le trascrizioni e pulisce i file temporanei.
        """
        logger.info(f"Starting advanced STT for file: {original_filename}")
        processed_audio_path = None
        segment_paths = []
        temp_dir_for_segments = None

        try:
            # 1. Pre-elaborazione
            processed_audio_path = await self._preprocess_audio(audio_file_path)
            if not processed_audio_path:
                return 500, None, "Audio preprocessing failed. Check if ffmpeg is installed."

            # 2. Controllo dimensione e segmentazione
            file_size_mb = os.path.getsize(processed_audio_path) / (1024 * 1024)
            if file_size_mb > MAX_AUDIO_SIZE_MB:
                logger.info(f"Preprocessed file is too large ({file_size_mb:.2f}MB). Splitting into segments.")
                segment_paths = await self._split_audio(processed_audio_path, STT_SEGMENT_DURATION_SECONDS)
                if not segment_paths:
                    return 500, None, "Failed to split large audio file."
                temp_dir_for_segments = os.path.dirname(segment_paths[0])
            else:
                logger.info(f"Preprocessed file size ({file_size_mb:.2f}MB) is within limits. Transcribing as a single file.")
                segment_paths = [processed_audio_path]

            # 3. Trascrizione (endpoint dedicato, un segmento alla volta)
            transcriptions = []
            for i, path in enumerate(segment_paths):
                segment_info = f"(Segment {i+1}/{len(segment_paths)})" if len(segment_paths) > 1 else ""
                text = await self._transcribe_segment_modern(path, f"{prompt} {segment_info}".strip(), language, model)
                if text:
                    transcriptions.append(text)

            # 4. Combinazione risultati: un segmento vuoto non deve far fallire tutto
            if not transcriptions:
                return 502, None, "Upstream transcription returned no text."
            full_transcription = "\n\n".join(transcriptions)
            
            logger.info(f"Successfully completed transcription for {original_filename}")
            return 200, {"transcription": full_transcription}, None

        except Exception as e:
            logger.exception(f"An unexpected error occurred during STT process for {original_filename}")
            return 500, None, f"An unexpected error in transcription pipeline: {e}"
        finally:
            # 5. Pulizia
            if processed_audio_path and os.path.exists(processed_audio_path) and not temp_dir_for_segments:
                os.remove(processed_audio_path) # Rimuovi solo se non è stato segmentato
            if temp_dir_for_segments and os.path.exists(temp_dir_for_segments):
                import shutil
                shutil.rmtree(temp_dir_for_segments, ignore_errors=True)
                logger.debug(f"Cleaned up temporary segment directory: {temp_dir_for_segments}")
