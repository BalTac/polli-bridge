"""Superficie OpenAI-compatibile.

Gli SDK/client OpenAI puntano qui cambiando solo la base_url:

    base_url = "http://<host>:8083/v1"

Ogni endpoint inoltra all upstream una sola richiesta, con token e referrer
già configurati nel client: nessun costo nascosto, nessuna logica duplicata.
Gli endpoint legacy (/generate, /text/*, /audio/*) restano vivi.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

import config
from models import (load_cached_models, save_models_to_cache,
                    load_cached_list, filter_models_by_category)

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def _as_list(entries):
    """La cache può contenere una lista oppure un dict con 'data'/'models'."""
    if isinstance(entries, list):
        return entries
    if isinstance(entries, dict):
        for key in ("data", "models"):
            value = entries.get(key)
            if isinstance(value, list):
                return value
    return []


def _openai_model(entry, kind):
    return {
        "id": entry.get("name"),
        "object": "model",
        "created": 0,
        "owned_by": entry.get("publisher") or "pollinations",
        "category": entry.get("category") or kind,
        "aliases": entry.get("aliases") or [],
        "pricing": entry.get("pricing"),
        # Surfaced so a client can tell free from paid before spending a call.
        "paid_only": bool(entry.get("paid_only")),
        "per_user_rpm": entry.get("per_user_rpm"),
    }


def create_router(client, sse_stream):
    """Costruisce il router /v1 (client = PollinationsClient, sse_stream = server._sse_stream)."""
    router = APIRouter(prefix="/v1", tags=["openai-compat"])

    @router.post("/chat/completions")
    async def chat_completions(request: Request):
        payload = await request.json()
        stream = bool(payload.get("stream"))
        status, data, content_type, error = await client.proxy_json(
            "post", "/v1/chat/completions", payload, stream=stream)
        if status != 200:
            raise HTTPException(status_code=status, detail=error or "upstream error")
        if stream:
            return sse_stream(data, "openai chat/completions")
        return JSONResponse(content=data)

    @router.post("/images/generations")
    async def images_generations(request: Request):
        payload = await request.json()
        payload.setdefault("model", "tongyi-mai/z-image-turbo")
        status, data, content_type, error = await client.proxy_json(
            "post", "/v1/images/generations", payload)
        if status != 200:
            raise HTTPException(status_code=status, detail=error or "upstream error")
        return JSONResponse(content=data)

    @router.post("/images/edits")
    async def images_edits(
        image: UploadFile = File(...),
        prompt: str = Form(...),
        model: str = Form("black-forest-labs/flux.2-klein-4b"),
        size: Optional[str] = Form(None),
    ):
        content = await image.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Immagine troppo grande (max 200 MB)")
        files = {"image": (image.filename or "image.png", content, image.content_type or "image/png")}
        fields = {"prompt": prompt, "model": model}
        if size:
            fields["size"] = size
        status, data, content_type, error = await client.proxy_multipart("/v1/images/edits", fields, files)
        if status != 200:
            raise HTTPException(status_code=status, detail=(error or "upstream error")[:500])
        return JSONResponse(content=data)

    @router.post("/audio/speech")
    async def audio_speech(request: Request):
        payload = await request.json()
        status, data, content_type, error = await client.proxy_json("post", "/v1/audio/speech", payload)
        if status != 200:
            raise HTTPException(status_code=status, detail=error or "upstream error")
        return Response(content=data, media_type=content_type or "audio/mpeg")

    @router.post("/audio/transcriptions")
    async def audio_transcriptions(
        file: UploadFile = File(...),
        model: str = Form("openai-audio"),
        language: Optional[str] = Form(None),
        prompt: Optional[str] = Form(None),
        response_format: str = Form("json"),
    ):
        content = await file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Audio troppo grande (max 200 MB)")
        files = {"file": (file.filename or "audio", content, file.content_type or "application/octet-stream")}
        fields = {"model": model, "response_format": response_format}
        if language:
            fields["language"] = language
        if prompt:
            fields["prompt"] = prompt
        status, data, content_type, error = await client.proxy_multipart("/v1/audio/transcriptions", fields, files)
        if status != 200:
            raise HTTPException(status_code=status, detail=(error or "upstream error")[:500])
        return Response(content=data, media_type=content_type or "application/json")

    @router.post("/audio/speech/with-timestamps")
    async def audio_speech_with_timestamps(request: Request):
        """TTS con timestamp: la coppia naturale dei sottotitoli."""
        payload = await request.json()
        status, data, content_type, error = await client.proxy_json(
            "post", "/v1/audio/speech/with-timestamps", payload)
        if status != 200 or data is None:
            raise HTTPException(status_code=status if status >= 400 else 500,
                                detail=(error or "upstream error")[:500])
        return JSONResponse(content=data)

    @router.get("/models")
    async def list_models():
        """Catalogo unificato in formato OpenAI: cache (TTL 24h) e fetch solo se vuota."""
        collected = []
        for kind, path in (("image", config.IMAGE_MODELS_FILE),
                           ("text", config.TEXT_MODELS_FILE),
                           ("decision", config.DECISION_MODELS_FILE)):
            entries = _as_list(load_cached_list(path) if kind == "decision" else load_cached_models(path))
            if not entries:
                if kind == "image":
                    fetched = await client.list_image_models()
                elif kind == "text":
                    fetched = await client.list_text_models()
                else:
                    fetched = await client.list_all_models()
                    if fetched[0] == 200 and isinstance(fetched[1], list):
                        save_models_to_cache(config.ALL_MODELS_FILE, fetched[1])
                        fetched = (200, filter_models_by_category(fetched[1], "decision"), None)
                if fetched[0] == 200 and fetched[1]:
                    save_models_to_cache(path, fetched[1])
                    entries = _as_list(fetched[1])
            for entry in entries:
                if isinstance(entry, dict) and entry.get("name"):
                    collected.append(_openai_model(entry, kind))
        unique = {}
        for item in collected:
            unique.setdefault(item["id"], item)
        return {"object": "list", "data": list(unique.values())}

    return router
