# server.py
# Main FastAPI application file for the Pollinations API server.

import os
import json
import logging
import tempfile
import shutil
import time
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, JSONResponse, Response, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
import aiofiles
import uvicorn

# Import configuration variables
import config

# Import the API client
from client import PollinationsClient, REQUEST_TOKEN
from openai_compat import create_router
from media import create_media_router

# Import model loading function
from models import (load_or_fetch_models, load_cached_models, save_models_to_cache,
                    load_cached_list, filter_models_by_category)

# --- Basic Logging Setup ---
# Configure logging based on settings in config.py
log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

# Log to file if LOG_FILE is set in config, otherwise log to stderr
log_file = config.LOG_FILE
if log_file:
    # Ensure log directory exists if specified in path
    log_dir = os.path.dirname(log_file)
    if (log_dir and not os.path.exists(log_dir)):
        try:
            os.makedirs(log_dir)
        except OSError as e:
            # Use standard print here as logging might not be configured yet
            print(f"ERROR: Could not create log directory {log_dir}: {e}")
            log_file = None # Fallback to console logging

# Use basicConfig - for more complex scenarios (like rotating logs), use handlers
logging.basicConfig(level=log_level,
                    format=log_format,
                    filename=log_file, # None logs to stderr
                    filemode='a' if log_file else 'w') # Append if file, write if console

# Get a logger instance for this module
logger = logging.getLogger(__name__)

logger.info("--- Starting Pollinations Server ---")
logger.info(f"Configuration - Host: {config.SERVER_HOST}, Port: {config.SERVER_PORT}")
logger.info(f"Configuration - Referrer: {config.DEFAULT_REFERRER}")
logger.info(f"Configuration - API Token: {'Configured' if config.POLLINATIONS_API_TOKEN else 'Not configured'}")
logger.info(f"Configuration - Log Level: {config.LOG_LEVEL}")
logger.info(f"Configuration - Log File: {config.LOG_FILE or 'Console'}")

# --- Initialize API Client ---
client = PollinationsClient(
    image_api_url=config.IMAGE_API_BASE_URL,
    text_api_url=config.TEXT_API_BASE_URL,
    referrer=config.DEFAULT_REFERRER,
    timeout=config.DEFAULT_API_TIMEOUT,
    api_token=config.POLLINATIONS_API_TOKEN
)
logger.info("PollinationsClient initialized.")

# --- Lifespan Event Handler ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — la validazione della configurazione sta FUORI dal try: se il
    # setup non e' sicuro il processo deve morire, non proseguire con un log.
    if AUTH_MODE == "server":
        if LOCAL_API_TOKEN:
            logger.info("Auth mode: server key + LOCAL_API_TOKEN (private instance).")
        elif not _LOOPBACK:
            raise RuntimeError(
                "POLLINATIONS_API_TOKEN is set but LOCAL_API_TOKEN is not, and the server "
                f"is bound to {config.SERVER_HOST}: refusing to start. Set LOCAL_API_TOKEN "
                "(recommended), bind to 127.0.0.1, or remove POLLINATIONS_API_TOKEN to run "
                "as a BYOP gateway.")
        else:
            logger.info("Auth mode: server key, loopback bind only (safe).")
    elif AUTH_MODE == "byop":
        logger.info("Auth mode: BYOP — every caller must send its own key "
                    "(Authorization: Bearer sk_...); no key -> 401. No server key is used.")
    else:
        logger.info("Auth mode: open on loopback — anonymous requests allowed, "
                    "a caller key is used when present.")

    try:
        # ffmpeg: basta guardare il PATH, senza lanciare un processo per scoprirlo
        if shutil.which("ffmpeg"):
            logger.info("ffmpeg disponibile - compressione e divisione audio attiva")
        else:
            logger.warning("ffmpeg non trovato - compressione e divisione audio non disponibili")

        # Load models asynchronously in the background
        image_models_list, text_models_list, voices_list = await load_or_fetch_models(
            image_fetch_func=client.list_image_models,
            text_fetch_func=client.list_text_models
        )
        logger.info(f"Successfully loaded models: {len(image_models_list)} image models")
    except Exception as e:
        logger.exception(f"Error loading models during startup: {e}")
    
    yield
    
    # Shutdown (if needed)
    logger.info("Application shutdown")

# --- Initialize FastAPI App ---
app = FastAPI(
    title="Pollinations API Server",
    description=(
        "OpenAI-compatible gateway in front of Pollinations: text, images, image edits, "
        "speech, transcription, video, 3D and embeddings behind one base_url. "
        "Self-hosted, one API key, no database."
    ),
    version="2.0.0",
    openapi_tags=[
        {"name": "openai-compat", "description": "The /v1 surface: drop-in for the OpenAI SDK."},
        {"name": "media", "description": "Native Pollinations extras: video, 3D, embeddings, jobs."},
        {"name": "meta", "description": "Health and service information."},
        {"name": "default", "description": "Legacy endpoints, kept for existing clients."},
    ],
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# --- CORS Middleware ---
app.add_middleware(
    CORSMiddleware,
    # Con allow_origins=["*"] i browser rifiutano allow_credentials=True: teniamolo spento.
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Process start time (used by /health) ---
# Cap sull'upload STT: oltre questa soglia si risponde 413 invece di riempire la RAM.
MAX_STT_UPLOAD_BYTES = 200 * 1024 * 1024

# --- Auth locale opzionale ---
# Se LOCAL_API_TOKEN e' impostata, gli endpoint (tranne i pubblici) la richiedono
# in X-API-Key oppure Authorization: Bearer. Se non e' impostata resta tutto
# aperto come prima: nessun chiamante esistente si rompe.
LOCAL_API_TOKEN = config.LOCAL_API_TOKEN
_PUBLIC_PATHS = {"/health", "/docs", "/redoc", "/swagger", "/openapi.json"}

# --- Modalita' di autenticazione: la decide la configurazione, non un flag ---
#   "server"  chiave sul server (istanza privata, eventualmente con LOCAL_API_TOKEN)
#   "open"    loopback senza chiave: si prova subito, la chiave del client e' opzionale
#   "byop"    esposta senza chiave: ogni chiamante porta la sua, senza -> 401
_LOOPBACK = config.SERVER_HOST in ("127.0.0.1", "localhost", "::1")
if config.POLLINATIONS_API_TOKEN:
    AUTH_MODE = "server"
elif _LOOPBACK:
    AUTH_MODE = "open"
else:
    AUTH_MODE = "byop"

START_TIME = time.time()


def _supplied_token(request: Request) -> str:
    """Chiave presentata dal chiamante: X-API-Key oppure Authorization: Bearer."""
    supplied = (request.headers.get("x-api-key") or "").strip()
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        supplied = header[7:].strip()
    return supplied


@app.middleware("http")
async def local_auth(request: Request, call_next):
    """Autenticazione secondo AUTH_MODE: chiave del server, del chiamante, o nessuna."""
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)

    supplied = _supplied_token(request)

    if AUTH_MODE == "server":
        if LOCAL_API_TOKEN and supplied != LOCAL_API_TOKEN:
            return JSONResponse({"detail": "Missing or invalid local API token"}, status_code=401)
        return await call_next(request)

    if AUTH_MODE == "byop" and not supplied:
        return JSONResponse(
            {"detail": "This gateway is bring-your-own-key: send your own Pollinations key as "
                       "Authorization: Bearer sk_yourkey (or X-API-Key). No server key is used."},
            status_code=401)

    if not supplied:
        return await call_next(request)

    ctx = REQUEST_TOKEN.set(supplied)
    try:
        return await call_next(request)
    finally:
        REQUEST_TOKEN.reset(ctx)


@app.get('/health', tags=['meta'])
async def health_check():
    """Liveness probe: nessuna chiamata upstream, risposta sempre immediata."""
    return {
        "status": "ok",
        "service": "pollinations-api-server",
        "version": app.version,
        "pid": os.getpid(),
        "bind": config.SERVER_HOST,
        "auth": AUTH_MODE if AUTH_MODE != "server" else ("enabled" if LOCAL_API_TOKEN else "disabled"),
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "upstream": {
            "image": config.IMAGE_API_BASE_URL,
            "text": config.TEXT_API_BASE_URL,
        },
        "endpoints": {
            "docs": "/docs",
            "redoc": "/redoc",
            "swagger": "/swagger",
            "openapi": "/openapi.json",
        },
        "timestamp": time.time(),
    }


@app.get('/swagger', include_in_schema=False)
async def swagger_alias():
    """Alias di /docs: stessa Swagger UI, stesso schema OpenAPI."""
    return RedirectResponse(url='/docs', status_code=307)


# --- SSE: un solo percorso per lo streaming ---
def _sse_stream(response_obj, label):
    """Stream SSE unico: chiude sempre response E sessione, anche in errore."""
    async def generator():
        try:
            async for chunk in response_obj.content:
                if chunk:
                    yield chunk
        except Exception as e:
            logger.exception(f"Errore durante lo stream SSE ({label}): {e}")
            payload = {"error": {"message": f"Stream error: {e}"}}
            yield f"data: {json.dumps(payload)}\n\n".encode('utf-8')
        finally:
            await response_obj.close()
            owner = getattr(response_obj, '_owner_session', None)
            if owner is not None:
                await owner.close()
            logger.debug(f"Stream SSE ({label}) chiuso, sessione inclusa.")
    return StreamingResponse(generator(), media_type='text/event-stream')




# --- Pydantic Models ---
class TextGenerationRequest(BaseModel):
    prompt: str = Field(..., description="The prompt for the text generation model.")
    model: str = "openai/gpt-5.4-nano"
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stream: bool = False
    json_mode: bool = False
    system: Optional[str] = None
    seed: Optional[int] = None

class TTSRequest(BaseModel):
    text: str
    voice: str
    model: str = "openai-audio"
    response_format: str = "mp3"
    private: bool = False
    enhance_text: bool = config.TTS_ENHANCE_TEXT_DEFAULT

# --- Helper Functions ---
def allowed_audio_file(filename):
    """Check if uploaded file has an allowed audio extension."""
    allowed_extensions = {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac', '.wma', '.opus'}
    _, ext = os.path.splitext(filename.lower())
    return ext in allowed_extensions

def _should_block_for_moderation(response_data: Dict[str, Any]) -> bool:
    """Evaluate moderation fields in a chat completion response.

    Returns True when the content should be blocked based on configured severity.
    Does not mutate the response.
    """
    if not isinstance(response_data, dict):
        return False

    severity_rank = {"safe": 0, "low": 1, "medium": 2, "high": 3}
    threshold = severity_rank.get(config.MODERATION_BLOCK_SEVERITY, 1)

    def is_blocking_result(result: Dict[str, Any]) -> bool:
        if not isinstance(result, dict):
            return False
        if result.get("filtered") is True:
            return True
        if result.get("detected") is True:
            return True
        severity = str(result.get("severity", "safe")).lower()
        return severity_rank.get(severity, 0) >= threshold

    # Check prompt_filter_results
    prompt_results = response_data.get("prompt_filter_results", [])
    if isinstance(prompt_results, list):
        for prompt_item in prompt_results:
            cfr = prompt_item.get("content_filter_results") if isinstance(prompt_item, dict) else None
            if isinstance(cfr, dict):
                for _, result in cfr.items():
                    if is_blocking_result(result):
                        return True

    # Check choices[].content_filter_results
    choices = response_data.get("choices", [])
    if isinstance(choices, list):
        for choice in choices:
            cfr = choice.get("content_filter_results") if isinstance(choice, dict) else None
            if isinstance(cfr, dict):
                for _, result in cfr.items():
                    if is_blocking_result(result):
                        return True

    return False

# --- FastAPI Routes ---

@app.get('/generate')
async def handle_generate_image_request(
    prompt: str = Query(..., description="Image generation prompt"),
    width: Optional[int] = Query(None, description="Image width"),
    height: Optional[int] = Query(None, description="Image height"),
    seed: Optional[int] = Query(None, description="Random seed"),
    model: str = Query("tongyi-mai/z-image-turbo", description="Image model (publisher/model o alias)"),
    enhance: bool = Query(False, description="Feature locale: riscrive il prompt con una chiamata LLM in piu (non e un parametro upstream)"),
    safe: bool = Query(False, description="Safe filter"),
    transparent: bool = Query(False, description="Transparent background"),
    json_response: bool = Query(False, alias="json", description="Return JSON response with base64 image and metadata")
):
    logger.info("Received request for /generate")


    # Call the client method asynchronously
    try:
        status_code, image_data, content_type, error = await client.generate_image(
            prompt=prompt, width=width, height=height, seed=seed,
            model=model, enhance=enhance, safe=safe,
            transparent=transparent, json=json_response
        )

        if status_code == 200 and image_data:
            logger.info("Image generated")
            
            # Handle JSON response vs binary response
            if json_response and content_type == "application/json":
                # Return JSON response
                return JSONResponse(content=image_data)
            else:
                # Return binary image
                return Response(content=image_data, media_type=content_type)
        else:
            logger.error(f"Failed to generate image (Status: {status_code}): {error}")
            raise HTTPException(status_code=status_code if status_code >= 400 else 500, 
                              detail=f"Image generation failed. {error}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Exception during image generation: {e}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

# --- Typed decisions (category: decision / "system one") -----------------
# Upstream shape: POST /alpha/decisions with {"state": ..., "questions": {...}}.
# Each question is one of three types: choice, score, noul.
# The upstream default model is `jev`, which is paid_only: true, so a caller
# without credits gets an error on the default path. We default to a free one.
DECISION_DEFAULT_MODEL = "microsoft/microsoft-decision-1.1"
DECISION_QUESTION_TYPES = ("choice", "score", "noul")


def _validate_decision_body(body):
    """Return the payload to forward, or raise 400 with a readable reason."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    if body.get("state") is None:
        raise HTTPException(status_code=400, detail="'state' is required")

    questions = body.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise HTTPException(status_code=400, detail="'questions' must be a non-empty object")

    for name, question in questions.items():
        if not isinstance(question, dict):
            raise HTTPException(status_code=400,
                                detail=f"questions.{name} must be an object")
        qtype = question.get("type")
        if qtype not in DECISION_QUESTION_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"questions.{name}.type must be one of {', '.join(DECISION_QUESTION_TYPES)}")

        # Upstream rejects a question without instructions with a generic
        # "JSON body validation failed": say it here, where it is actionable.
        instructions = question.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise HTTPException(status_code=400,
                                detail=f"questions.{name}.instructions is required")

    payload = dict(body)
    payload["model"] = body.get("model") or DECISION_DEFAULT_MODEL
    return payload


async def _forward_decision(body):
    payload = _validate_decision_body(body)
    logger.info(f"Forwarding decision request (model={payload['model']}, "
                f"questions={len(payload['questions'])})")

    status, data, content_type, error = await client.proxy_json(
        "post", "/alpha/decisions", payload)
    if status != 200 or data is None:
        raise HTTPException(status_code=status if status >= 400 else 500,
                            detail=(error or "upstream error")[:500])
    return JSONResponse(content=data)


@app.post('/alpha/decisions', tags=['decisions'])
async def handle_decisions(request: Request):
    """Native decisions endpoint: calibrated choice/score/noul answers."""
    logger.info("Received POST request for /alpha/decisions")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    return await _forward_decision(body)


@app.post('/v1/systemone', tags=['decisions'])
async def handle_systemone(request: Request):
    """TypeSafe-compatible alias of /alpha/decisions (same request, same answer)."""
    logger.info("Received POST request for /v1/systemone")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    return await _forward_decision(body)


@app.get('/models/image')
async def handle_list_image_models():
    """Catalogo immagini: cache su file (TTL 24h), upstream solo se scaduta."""
    cached = load_cached_models(config.IMAGE_MODELS_FILE)
    if cached:
        return cached
    status_code, models_data, error = await client.list_image_models()
    if status_code == 200 and models_data is not None:
        save_models_to_cache(config.IMAGE_MODELS_FILE, models_data)
        return models_data
    raise HTTPException(status_code=status_code if status_code >= 400 else 500,
                        detail=f"Failed to list image models. {error}")


@app.get('/models/text')
async def handle_list_text_models():
    """Catalogo testo/voci: cache su file (TTL 24h), upstream solo se scaduta."""
    cached = load_cached_models(config.TEXT_MODELS_FILE)
    if cached:
        return cached
    status_code, models_data, error = await client.list_text_models()
    if status_code == 200 and models_data is not None:
        save_models_to_cache(config.TEXT_MODELS_FILE, models_data)
        return models_data
    raise HTTPException(status_code=status_code if status_code >= 400 else 500,
                        detail=f"Failed to list text models. {error}")


@app.get('/models/decision')
async def handle_list_decision_models():
    """Catalogo decision (system-one): filtrato dal catalogo unificato, cache 24h."""
    cached = load_cached_list(config.DECISION_MODELS_FILE)
    if cached:
        return cached

    status_code, models_data, error = await client.list_all_models()
    if status_code == 200 and isinstance(models_data, list):
        save_models_to_cache(config.ALL_MODELS_FILE, models_data)
        decision_models = filter_models_by_category(models_data, "decision")
        save_models_to_cache(config.DECISION_MODELS_FILE, decision_models)
        return decision_models

    raise HTTPException(status_code=status_code if status_code >= 400 else 500,
                        detail=f"Failed to list decision models. {error}")


@app.get('/account/balance')
async def handle_account_balance():
    logger.info("Received request for /account/balance")

    try:
        status_code, response_data, error = await client.get_account_balance()

        if status_code == 200 and response_data is not None:
            return response_data
        else:
            logger.error(f"Failed to get account balance (Status: {status_code}): {error}")
            raise HTTPException(status_code=status_code if status_code >= 400 else 500,
                              detail=f"Failed to get account balance. {error}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Exception during account balance retrieval: {e}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@app.get('/text/generate_get')
async def handle_generate_text_get(
    prompt: str = Query(..., description="Text generation prompt"),
    model: str = Query("openai", description="Text model"),
    seed: Optional[int] = Query(None, description="Random seed"),
    json_response: bool = Query(False, alias="json", description="JSON output"),
    system: Optional[str] = Query(None, description="System prompt"),
    stream: bool = Query(False, description="Enable streaming")
):
    logger.info("Received request for /text/generate_get")


    try:
        # --- Handle Streaming ---
        if (stream):
            logger.info(f"Initiating stream for text GET prompt: '{prompt[:50]}...'")
            
            status_code, response_obj, content_type, error = await client.generate_text_get(
                prompt=prompt, model=model, seed=seed, json=json_response,
                system=system, stream=True
            )

            if status_code == 200 and response_obj:
                return _sse_stream(response_obj, "text GET")
            else:
                logger.error(f"Failed to initiate text GET stream (Status: {status_code}): {error}")
                raise HTTPException(status_code=status_code if status_code >= 400 else 500, 
                                  detail=f"Failed to start text stream. {error}")

        # --- Handle Non-Streaming ---
        else:
            status_code, response_data, content_type, error = await client.generate_text_get(
                prompt=prompt, model=model, seed=seed, json=json_response, 
                system=system, stream=False
            )

            if status_code == 200 and response_data is not None:
                logger.info("Text generated (GET)")
                mime = 'application/json' if json_response and isinstance(response_data, str) else 'text/plain'
                return Response(content=response_data, media_type=mime)
            else:
                logger.error(f"Failed to generate text (GET) (Status: {status_code}): {error}")
                raise HTTPException(status_code=status_code if status_code >= 400 else 500, 
                                  detail=f"Text generation (GET) failed. {error}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Exception during text generation (GET): {e}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@app.post('/text/generate_post')
async def handle_generate_text_post(request: TextGenerationRequest):
    logger.info("Received POST request for /text/generate_post")

    payload = request.dict()
    
    try:
        # Determine the type of request based on payload structure
        is_vision_request = False
        is_stt_request = False

        if isinstance(payload.get('messages'), list):
            for message in payload['messages']:
                if isinstance(message.get('content'), list):
                    for content_item in message['content']:
                        if isinstance(content_item, dict):
                            if content_item.get('type') == 'image_url':
                                is_vision_request = True
                                break
                            if content_item.get('type') == 'input_audio':
                                is_stt_request = True
                                break
                if is_vision_request or is_stt_request:
                    break

        # Handle different request types
        if is_vision_request:
            logger.info("Handling /text/generate_post as VISION request.")
            status_code, response_data, content_type, error = await client.generate_text_post(payload=payload, stream=False)
            
            if status_code == 200 and response_data:
                logger.info("Vision request successful.")
                if _should_block_for_moderation(response_data):
                    logger.warning("Vision response blocked by moderation policy.")
                    raise HTTPException(status_code=403, detail="Content blocked by moderation policy")
                return response_data
            else:
                logger.error(f"Vision request failed (Status: {status_code}): {error}")
                raise HTTPException(status_code=status_code if status_code >= 400 else 500, detail=error or "Vision request failed")

        elif is_stt_request:
            logger.info("Handling /text/generate_post as STT request.")
            status_code, response_data, content_type, error = await client.generate_text_post(payload=payload, stream=False)
            
            if status_code == 200 and response_data:
                logger.info("STT request successful.")
                if _should_block_for_moderation(response_data):
                    logger.warning("STT response blocked by moderation policy.")
                    raise HTTPException(status_code=403, detail="Content blocked by moderation policy")
                return response_data
            else:
                logger.error(f"STT request failed (Status: {status_code}): {error}")
                raise HTTPException(status_code=status_code if status_code >= 400 else 500, detail=error or "STT request failed")

        else: # Standard text chat completion
            # Convert json_mode back to json for API compatibility
            if 'json_mode' in payload:
                payload['json'] = payload.pop('json_mode')
            
            stream_requested = payload.get('stream', False)
            logger.info(f"Handling /text/generate_post as TEXT CHAT request (stream: {stream_requested}).")
            
            if stream_requested:
                status_code, response_obj, content_type, error = await client.generate_text_post(payload=payload, stream=True)

                if status_code == 200 and response_obj:
                    return _sse_stream(response_obj, "text POST")
                else:
                    logger.error(f"Failed to initiate stream for text POST (Status: {status_code}): {error}")
                    raise HTTPException(status_code=status_code if status_code >= 400 else 500, 
                                      detail=error or "Failed to initiate stream")
            else: # Non-streaming text chat
                status_code, response_data, content_type, error = await client.generate_text_post(payload=payload, stream=False)
                
                if status_code == 200 and response_data:
                    logger.info("Text POST (non-stream) request successful.")
                    if _should_block_for_moderation(response_data):
                        logger.warning("Text response blocked by moderation policy.")
                        raise HTTPException(status_code=403, detail="Content blocked by moderation policy")
                    return response_data
                else:
                    logger.error(f"Text POST (non-stream) request failed (Status: {status_code}): {error}")
                    raise HTTPException(status_code=status_code if status_code >= 400 else 500, 
                                      detail=error or "Text POST request failed")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Exception during text generation (POST): {e}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@app.get('/audio/tts_get')
async def handle_tts_get(
    text: str = Query(..., description="Text to synthesize"),
    voice: str = Query("alloy", description="Voice to use"),
    enhance_text: bool = Query(config.TTS_ENHANCE_TEXT_DEFAULT, description="Enhance text quality before TTS"),
    language: Optional[str] = Query(None, description="Language of the text (e.g., 'it', 'en')")
):
    logger.info(f"Received request for /audio/tts_get: text='{text[:50]}...', enhance_text={enhance_text}")

    if not text:
        logger.warning("Bad request to /audio/tts_get: missing 'text'.")
        raise HTTPException(status_code=400, detail="'text' parameter is required.")

    # Valida la voce
    available_voices = ["alloy", "echo", "fable", "onyx", "nova", "shimmer", "coral", "verse", "ballad", "ash", "sage", "amuch", "dan"]
    if voice not in available_voices:
        voice = "alloy"

    try:
        # Use our intelligent client's POST method for all advanced features
        # This enables: tier-aware splitting, text enhancement, content filtering fallback
        # The POST method handles both short and long text automatically
        
        status_code, audio_data, content_type, error = await client.generate_audio_tts_post(
            text=text,
            voice=voice,
            model="openai-audio",
            response_format="mp3",
            private=False,
            language=language,
            enhance_text=enhance_text
        )
        
        if status_code == 200 and audio_data:
            logger.info(f"Successfully generated TTS audio (GET->POST) for voice: {voice}")
            return Response(content=audio_data, media_type=content_type)
        else:
            logger.error(f"Failed to generate TTS audio (GET->POST) (Status: {status_code}): {error}")
            raise HTTPException(status_code=status_code if status_code >= 400 else 500, 
                              detail=f"TTS audio generation failed. {error}")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Exception during TTS generation (GET): {e}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@app.post('/audio/tts_post')
async def handle_tts_post(request: TTSRequest):
    logger.info("Received POST request for /audio/tts_post")

    try:
        status_code, audio_data, content_type, error = await client.generate_audio_tts_post(
            text=request.text, 
            voice=request.voice, 
            model=request.model, 
            response_format=request.response_format, 
            private=request.private,
            enhance_text=request.enhance_text  # Pass the new parameter
        )

        if status_code == 200 and audio_data:
            logger.info(f"Successfully generated TTS audio (POST) for voice: {request.voice}")
            return Response(content=audio_data, media_type=content_type)
        else:
            logger.error(f"Failed to generate TTS audio (POST) (Status: {status_code}): {error}")
            raise HTTPException(status_code=status_code if status_code >= 400 else 500, 
                              detail=f"TTS audio generation (POST) failed. {error}")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Exception during TTS generation (POST): {e}")
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

@app.post('/audio/stt_file_upload')
async def handle_audio_stt_file_upload(
    audio_file: UploadFile = File(...),
    prompt: str = Form("Trascrivi questo audio."),
    language: Optional[str] = Form(None),
    private: bool = Form(False),
    model: str = Form("openai/whisper-large-v3"),
    diarize: bool = Form(False)
):
    """Handles audio file upload for Speech-to-Text transcription."""
    logger.debug(f"Received audio file: {audio_file.filename}, Content-Type: {audio_file.content_type}")
    logger.debug(f"Prompt: '{prompt}', Language: {language}, Private: {private}, Diarize: {diarize}")
    
    if not audio_file.filename:
        raise HTTPException(status_code=400, detail="No selected audio file")

    if not allowed_audio_file(audio_file.filename):
        raise HTTPException(status_code=400, detail="Invalid audio file type")

    logger.info(f"Received audio file for STT: {audio_file.filename}")

    # Save to a temporary file asynchronously
    try:
        temp_dir = tempfile.gettempdir()
        _, ext = os.path.splitext(audio_file.filename)
        
        # File temporaneo chiuso subito: nessun descriptor lasciato aperto.
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir=temp_dir) as tmp:
            temp_audio_file_path = tmp.name

        # Copia a blocchi: un upload da 300 MB non finisce tutto in RAM.
        total_bytes = 0
        too_big = False
        async with aiofiles.open(temp_audio_file_path, 'wb') as f:
            while True:
                chunk = await audio_file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_STT_UPLOAD_BYTES:
                    too_big = True
                    break
                await f.write(chunk)
        if too_big:
            os.remove(temp_audio_file_path)
            raise HTTPException(
                status_code=413,
                detail=f"Audio troppo grande: massimo {MAX_STT_UPLOAD_BYTES // (1024 * 1024)} MB")

        logger.debug(f"Audio file saved temporarily to: {temp_audio_file_path}")
        logger.debug(f"File size: {total_bytes} bytes")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving temporary audio file: {e}")
        raise HTTPException(status_code=500, detail=f"Could not save temporary audio file: {e}")
        
    # Log the parameters being passed to the client method
    logger.debug(f"Calling transcribe_audio_file_stt with params: path={temp_audio_file_path}, filename={audio_file.filename}, prompt='{prompt}', model={model}, language={language}, private={private}, diarize={diarize}")
        
    try:
        # Call the client method
        status_code, result_data, error_message = await client.transcribe_audio_file_stt(
            audio_file_path=temp_audio_file_path,
            original_filename=audio_file.filename,
            prompt=prompt,
            language=language,
            private=private,
            model=model,
            diarize=diarize
        )
        
        # Clean up the temporary file asynchronously
        try:
            os.remove(temp_audio_file_path)
            logger.debug(f"Cleaned up temporary audio file: {temp_audio_file_path}")
        except OSError as e:
            logger.error(f"Error deleting temporary audio file {temp_audio_file_path}: {e}")

        logger.debug(f"STT API response: status={status_code}, result_data={result_data}, error={error_message}")
        
        if error_message:
            raise HTTPException(status_code=status_code if status_code >= 400 else 500, detail=error_message)
        
        if status_code == 200 and result_data:
            return result_data # result_data should be {"transcription": "..."}
        else:
            raise HTTPException(status_code=status_code if status_code >= 400 else 500, 
                              detail="STT request failed or returned unexpected data")
    except HTTPException:
        raise
    except Exception as e:
        # Clean up the temporary file in case of error
        try:
            os.remove(temp_audio_file_path)
            logger.debug(f"Cleaned up temporary audio file after error: {temp_audio_file_path}")
        except OSError:
            pass
            
        logger.exception(f"Exception during STT processing: {e}")
        raise HTTPException(status_code=500, detail=f"STT processing error: {str(e)}")

@app.get("/health/transcription")
async def health_check_transcription():
    """Check transcription service health using lightweight models endpoint."""
    try:
        # Test the models endpoint first - this is very lightweight and reliable
        models_url = f"{client.text_api_url}/models"
        
        status_code, response_data, content_type, error = await client._make_request(
            'get', models_url, timeout=10
        )
        
        if status_code == 200 and response_data:
            # Check if audio model is available
            models = response_data if isinstance(response_data, list) else []
            audio_model_available = any('openai-audio' in str(model).lower() for model in models)
            
            if audio_model_available:
                return {
                    "status": "healthy",
                    "message": "Transcription service is operational",
                    "timestamp": time.time(),
                    "audio_model_available": True,
                    "models_endpoint": "accessible"
                }
            else:
                # Models endpoint works but no audio model found
                return {
                    "status": "degraded", 
                    "message": "Service responding but audio model not confirmed",
                    "timestamp": time.time(),
                    "audio_model_available": False,
                    "models_endpoint": "accessible"
                }
        
        elif status_code in [500, 502, 503, 504]:
            return {
                "status": "unhealthy",
                "message": f"Service error: {status_code}",
                "error": str(error),
                "timestamp": time.time(),
                "models_endpoint": "error"
            }
        else:
            return {
                "status": "unknown",
                "message": f"Unexpected response: {status_code}",
                "error": str(error),
                "timestamp": time.time(),
                "models_endpoint": "unexpected"
            }
            
    except Exception as e:
        return {
            "status": "error",
            "message": "Health check failed with exception",
            "error": str(e),
            "timestamp": time.time(),
            "models_endpoint": "unreachable"
        }

# --- Superficie OpenAI-compatibile (/v1/*), additiva rispetto ai legacy ---
app.include_router(create_router(client, _sse_stream))

# --- Video, 3D, embeddings e job: capacità native, passthrough ---
app.include_router(create_media_router(client))

# --- Main Execution Block ---
if __name__ == "__main__":
    if config.ENABLE_DEBUG:
        # reload richiede per forza una stringa di import
        uvicorn.run("server:app", host=config.SERVER_HOST, port=config.SERVER_PORT,
                    reload=True, log_level=config.LOG_LEVEL.lower())
    else:
        # Passando l oggetto app il modulo non viene importato una seconda volta
        # (prima: __main__ + server = due client, due init della config).
        uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT,
                    reload=False, log_level=config.LOG_LEVEL.lower())

