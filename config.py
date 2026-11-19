# config.py
# Configuration settings for the Pollinations API Server

import os
from dotenv import load_dotenv
from typing import Dict, Any

# Load environment variables from .env file
load_dotenv()

# --- API Configuration ---
# Base URL for the Pollinations API gateway (new reference)
# Both image and text endpoints are now served from the same base.
API_BASE_URL: str = os.getenv("POLLINATIONS_API_BASE_URL", "https://gen.pollinations.ai")
# Alias storici usati dal client: la verita' resta una sola.
IMAGE_API_BASE_URL: str = API_BASE_URL
TEXT_API_BASE_URL: str = API_BASE_URL

# Default referrer identifier for API calls
# You might want to customize this or load it from environment variables
DEFAULT_REFERRER: str = os.getenv("POLLINATIONS_REFERRER", "polli-bridge/2.0.0")

# Pollinations API Token for priority access (optional)
POLLINATIONS_API_TOKEN: str | None = os.getenv("POLLINATIONS_API_TOKEN", None)

# Optional local auth: when set, non-public endpoints require it.
LOCAL_API_TOKEN: str | None = os.getenv("LOCAL_API_TOKEN", None)

# --- Server Configuration ---
# Host for the Flask server to bind to.
# '0.0.0.0' makes it accessible on the local network.
SERVER_HOST: str = os.getenv("SERVER_HOST", "127.0.0.1")
# Port for the Flask server
SERVER_PORT: int = int(os.getenv("SERVER_PORT", 8083))

# --- Model Cache Configuration ---
# Directory to store cached model data
MODELS_DATA_DIR: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_data")
IMAGE_MODELS_FILE: str = os.path.join(MODELS_DATA_DIR, "image_models.json")
TEXT_MODELS_FILE: str = os.path.join(MODELS_DATA_DIR, "text_models.json")

# --- Logging Configuration ---
# (We will add more specific logging config later when implementing the logging module)
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO") # e.g., DEBUG, INFO, WARNING, ERROR
LOG_FILE: str | None = os.getenv("LOG_FILE") # e.g., /var/log/pollinations_server.log or None to log to console/journal

# --- Feature Flags / Limits ---
# Enable detailed debug messages (can be overridden by environment variable)
ENABLE_DEBUG: bool = os.getenv("ENABLE_DEBUG", "False").lower() == "true"

# Timeout for API requests in seconds (adjust as needed)
DEFAULT_API_TIMEOUT: int = 300 # Increased default timeout

# --- TTS Tier-Aware Configuration ---
# Define the current usage tier: 'seed', 'nectar', 'flower'
USAGE_TIER: str = os.getenv("POLLINATIONS_TIER", "seed").lower()

# Default setting for enhancing text quality before TTS generation
TTS_ENHANCE_TEXT_DEFAULT: bool = os.getenv("TTS_ENHANCE_TEXT_DEFAULT", "true").lower() == "true"

# Optional override for TTS chunk size (0 = use tier defaults)
TTS_CHUNK_MAX_CHARS: int = int(os.getenv("TTS_CHUNK_MAX_CHARS", "0"))

# --- Moderation Configuration ---
# Block responses when moderation severity is at/above this level.
# Allowed: safe, low, medium, high
MODERATION_BLOCK_SEVERITY: str = os.getenv("MODERATION_BLOCK_SEVERITY", "low").lower()

# Dictionary holding tier-specific limits and settings
TTS_TIER_CONFIG: Dict[str, Dict[str, Any]] = {
    "seed": {
        "max_chars_per_request": 300,   # Approx. 20 seconds of audio
        "delay_between_requests": 3.0,  # 1 request every 3 seconds
        "description": "Basic tier with strict limits."
    },
    "nectar": {
        "max_chars_per_request": 900,   # Approx. 60 seconds of audio
        "delay_between_requests": 1.0,  # 1 request per second
        "description": "Intermediate tier."
    },
    "flower": {
        "max_chars_per_request": 1800,  # Approx. 120 seconds of audio
        "delay_between_requests": 0.1,  # 10 requests per second
        "description": "Highest tier with minimal limits."
    }
}

# Get the configuration for the current tier
CURRENT_TIER_CONFIG = TTS_TIER_CONFIG.get(USAGE_TIER, TTS_TIER_CONFIG["seed"])
if USAGE_TIER not in TTS_TIER_CONFIG:
    print(f"Warning: Unknown USAGE_TIER '{USAGE_TIER}'. Falling back to 'seed' configuration.")

# Audio processing settings (if needed by server endpoints)
# Approximate size limit in MB before suggesting/applying compression (relevant if server handles STT logic)
# AUDIO_SIZE_LIMIT_MB: int = 4
# Default audio compression bitrate (relevant if server handles STT logic)
# COMPRESS_BITRATE: str = "64k"
# Default audio split duration in seconds (relevant if server handles STT logic)
# SPLIT_DURATION: int = 60

# --- Add other configurations as needed ---

# Nessuna stampa all'import: niente segreti nei log.
# Il riepilogo di avvio lo emette server.py via logger, senza token.
