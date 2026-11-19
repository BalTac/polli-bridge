# models.py
# Functions for loading and caching model information

import os
import json
import logging
from datetime import datetime, timedelta

# Import configuration
import config

logger = logging.getLogger(__name__)

# Set up models data directory if it doesn't exist
if not os.path.exists(config.MODELS_DATA_DIR):
    try:
        os.makedirs(config.MODELS_DATA_DIR)
        logger.info(f"Created models data directory: {config.MODELS_DATA_DIR}")
    except OSError as e:
        logger.error(f"Could not create models data directory: {e}")

async def load_or_fetch_models(image_fetch_func=None, text_fetch_func=None, force_refresh=False):
    """
    Load models from cache or fetch them if needed.
    
    Args:
        image_fetch_func: Async function to fetch image models
        text_fetch_func: Async function to fetch text models
        force_refresh: Whether to force a refresh of cached data
        
    Returns:
        tuple: (image_models_list, text_models_list, voices_list)
    """
    image_models = []
    text_models = {}
    voices = []
    
    # 1. Try to load from cache
    if not force_refresh:
        image_models = load_cached_models(config.IMAGE_MODELS_FILE)
        text_data = load_cached_models(config.TEXT_MODELS_FILE)
        
        # Extract text models and voices if available
        if isinstance(text_data, dict):
            text_models = text_data
            
            # Try to extract voices (might be in different formats depending on API response)
            if 'openai-audio' in text_data and 'voices' in text_data['openai-audio']:
                voices = text_data['openai-audio'].get('voices', [])
            elif 'voices' in text_data:
                voices = text_data.get('voices', [])
        elif isinstance(text_data, list):
            text_models = text_data
            # Try to collect voices from model entries (new API format)
            for model_entry in text_data:
                if isinstance(model_entry, dict) and 'voices' in model_entry:
                    voices.extend(model_entry.get('voices', []) or [])
            voices = list(dict.fromkeys(voices))
    
    # 2. If cache is empty, expired, or forced refresh, fetch new data
    need_fetch_image = not image_models or force_refresh
    need_fetch_text = not text_models or force_refresh
    
    # 3. Fetch and save model data if needed
    if need_fetch_image and image_fetch_func:
        logger.info("Fetching image models from API...")
        status_code, models_data, error = await image_fetch_func()
        
        if status_code == 200 and models_data:
            image_models = models_data
            save_models_to_cache(config.IMAGE_MODELS_FILE, models_data)
            logger.info(f"Updated image models cache with {len(image_models)} models")
        else:
            logger.error(f"Failed to fetch image models: {error}")
    
    if need_fetch_text and text_fetch_func:
        logger.info("Fetching text models from API...")
        status_code, models_data, error = await text_fetch_func()
        
        if status_code == 200 and models_data:
            text_models = models_data
            save_models_to_cache(config.TEXT_MODELS_FILE, models_data)
            logger.info(f"Updated text models cache")
            
            # Extract voices from response
            if isinstance(models_data, dict):
                if 'openai-audio' in models_data and 'voices' in models_data['openai-audio']:
                    voices = models_data['openai-audio'].get('voices', [])
                elif 'voices' in models_data:
                    voices = models_data.get('voices', [])
            elif isinstance(models_data, list):
                for model_entry in models_data:
                    if isinstance(model_entry, dict) and 'voices' in model_entry:
                        voices.extend(model_entry.get('voices', []) or [])
                voices = list(dict.fromkeys(voices))
            
            if voices:
                logger.info(f"Extracted {len(voices)} voices from text models data")
        else:
            logger.error(f"Failed to fetch text models: {error}")
    
    # Convert to list format if necessary
    image_models_list = image_models if isinstance(image_models, list) else []
    
    return image_models_list, text_models, voices

def load_cached_models(file_path, max_age_hours=24):
    """
    Load models from cache if available and not too old.
    
    Args:
        file_path: Path to the cached models file
        max_age_hours: Maximum age of cache in hours
        
    Returns:
        list or dict: Loaded models data, or empty if cache is invalid
    """
    if not os.path.exists(file_path):
        logger.debug(f"Cache file does not exist: {file_path}")
        return [] if file_path.endswith("image_models.json") else {}
    
    # Check file age
    file_time = datetime.fromtimestamp(os.path.getmtime(file_path))
    if datetime.now() - file_time > timedelta(hours=max_age_hours):
        logger.info(f"Cache is older than {max_age_hours} hours, will refresh: {file_path}")
        return [] if file_path.endswith("image_models.json") else {}
    
    # Load the data
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
            cache_type = "image" if file_path.endswith("image_models.json") else "text"
            logger.debug(f"Loaded {cache_type} models from cache: {file_path}")
            return data
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading cached models from {file_path}: {e}")
        return [] if file_path.endswith("image_models.json") else {}

def save_models_to_cache(file_path, models_data):
    """
    Save models data to cache file.
    
    Args:
        file_path: Path to save the models data
        models_data: Models data to save
    """
    try:
        # Ensure directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        # Write data to file
        with open(file_path, 'w') as f:
            json.dump(models_data, f)
        logger.debug(f"Saved models data to cache: {file_path}")
    except (IOError, OSError) as e:
        logger.error(f"Error saving models to cache {file_path}: {e}")

