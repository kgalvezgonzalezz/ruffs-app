"""RUFFS: voz local (Vosk) — ver src/voice_local.py."""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user

MAX_CHUNK_BYTES = 16000 * 2 * 5   # 5 s de audio por petición como máximo


def _stt_language() -> str:
    try:
        from src.settings import load_settings
        return (load_settings() or {}).get("stt_language", "") or ""
    except Exception:
        return ""


def setup_voice_routes() -> APIRouter:
    router = APIRouter(prefix="/api/voice", tags=["voice"])
    from src import voice_local as vl

    @router.get("/status")
    async def voice_status(request: Request, lang: str = ""):
        require_user(request)
        return vl.status(lang or _stt_language())

    @router.post("/model")
    async def download_model(request: Request):
        require_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not vl.engine_available():
            raise HTTPException(503, "El motor de voz local no está instalado (vosk)")
        return vl.start_download(body.get("lang") or _stt_language())

    @router.post("/session")
    async def open_session(request: Request):
        owner = require_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        lang = body.get("lang") or _stt_language()
        try:
            sid = await asyncio.to_thread(vl.open_session, owner, body.get("mode", "dictate"), lang,
                                          (body.get("keyword") or "RUFFS")[:40])
        except vl.VoiceError as e:
            code = 409 if str(e) in ("model_missing", "engine_missing") else 400
            raise HTTPException(code, str(e))
        return {"id": sid, "sample_rate": vl.SAMPLE_RATE, "lang": vl.model_lang(lang)}

    @router.post("/session/{sid}/audio")
    async def audio(sid: str, request: Request):
        owner = require_user(request)
        pcm = await request.body()
        if len(pcm) > MAX_CHUNK_BYTES:
            raise HTTPException(413, "Trozo de audio demasiado grande")
        try:
            return await asyncio.to_thread(vl.feed, sid, owner, pcm)
        except vl.VoiceError as e:
            raise HTTPException(404, str(e))

    @router.post("/session/{sid}/end")
    async def end(sid: str, request: Request):
        owner = require_user(request)
        try:
            return {"text": await asyncio.to_thread(vl.finish, sid, owner)}
        except vl.VoiceError as e:
            raise HTTPException(404, str(e))

    @router.delete("/session/{sid}")
    async def cancel(sid: str, request: Request):
        owner = require_user(request)
        vl.close(sid, owner)
        return {"ok": True}

    return router
