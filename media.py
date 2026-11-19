"""Capacità native Pollinations che prima non esistevano: video, 3D, embeddings, job.

Sono passthrough sottili: token, referrer e timeout lungo dove serve.
Nessuna trasformazione: se upstream cambia, qui non si rompe niente.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

VIDEO_TIMEOUT = 900      # la generazione video può durare minuti
THREED_TIMEOUT = 600     # 3D idem, un po' meno


def _clean(params):
    """Tiene solo i parametri valorizzati: niente ?model= vuoti a upstream."""
    return {k: v for k, v in params.items() if v is not None}


def create_media_router(client):
    router = APIRouter(tags=["media"])

    async def _json(path, label, params=None, timeout=None):
        status, data, content_type, error = await client.proxy_get(path, params=params, timeout=timeout)
        if status != 200 or data is None:
            raise HTTPException(status_code=status if status >= 400 else 500,
                                detail=(error or f"{label}: upstream error")[:500])
        return JSONResponse(content=data)

    async def _binary(path, params, timeout, fallback_type, label):
        status, data, content_type, error = await client.proxy_get(path, params=params, timeout=timeout)
        if status != 200 or data is None:
            raise HTTPException(status_code=status if status >= 400 else 500,
                                detail=(error or f"{label}: upstream error")[:500])
        return Response(content=data, media_type=content_type or fallback_type)

    # --- Cataloghi (dichiarati prima delle rotte con {prompt}) ---
    @router.get("/video/models")
    async def video_models():
        return await _json("/video/models", "catalogo video")

    @router.get("/3d/models")
    async def threed_models():
        return await _json("/3d/models", "catalogo 3D")

    @router.get("/embeddings/models")
    async def embeddings_models():
        return await _json("/embeddings/models", "catalogo embeddings")

    # --- Generazione ---
    @router.get("/video/{prompt}")
    async def generate_video(
        prompt: str,
        model: Optional[str] = Query(None, description="publisher/model o alias"),
        duration: Optional[int] = Query(None, description="Durata in secondi"),
        aspectRatio: Optional[str] = Query(None, description="es. 16:9"),
        resolution: Optional[str] = Query(None),
        seed: Optional[int] = Query(None),
        image: Optional[str] = Query(None, description="URL immagine di partenza (start frame)"),
        safe: Optional[bool] = Query(None),
    ):
        params = _clean({"model": model, "duration": duration, "aspectRatio": aspectRatio,
                         "resolution": resolution, "seed": seed, "image": image,
                         "safe": "true" if safe else None})
        return await _binary(f"/video/{quote(prompt)}", params, VIDEO_TIMEOUT, "video/mp4", "video")

    @router.get("/3d/{prompt}")
    async def generate_3d(
        prompt: str,
        model: Optional[str] = Query(None, description="es. microsoft/trellis-2"),
        image: Optional[str] = Query(None, description="Immagine sorgente"),
        resolution: Optional[str] = Query(None, description="low | medium | high"),
        seed: Optional[int] = Query(None),
        safe: Optional[bool] = Query(None),
    ):
        params = _clean({"model": model, "image": image, "resolution": resolution,
                         "seed": seed, "safe": "true" if safe else None})
        return await _binary(f"/3d/{quote(prompt)}", params, THREED_TIMEOUT,
                             "model/gltf-binary", "3d")

    @router.post("/v1/embeddings")
    async def embeddings(request: Request):
        payload = await request.json()
        status, data, content_type, error = await client.proxy_json("post", "/v1/embeddings", payload)
        if status != 200 or data is None:
            raise HTTPException(status_code=status if status >= 400 else 500,
                                detail=(error or "embeddings: upstream error")[:500])
        return JSONResponse(content=data)

    # --- Job asincroni (per ciò che upstream accoda) ---
    @router.get("/v1/jobs/{job_id}/result")
    async def job_result(job_id: str):
        return await _json(f"/v1/jobs/{quote(job_id)}/result", "job result", timeout=VIDEO_TIMEOUT)

    @router.get("/v1/jobs/{job_id}")
    async def job_status(job_id: str):
        return await _json(f"/v1/jobs/{quote(job_id)}", "job status")

    return router
