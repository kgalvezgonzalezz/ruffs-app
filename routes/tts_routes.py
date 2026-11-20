# routes/tts_routes.py
"""
TTS API routes — multi-provider (local Kokoro, API endpoint, browser).
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)

class TTSRequest(BaseModel):
    text: str
    format: str = "audio"  # "audio" or "base64"
    provider: Optional[str] = None  # Override: use this provider instead of settings
    voice: Optional[str] = None     # Override: use this voice
    speed: Optional[float] = None   # Override: use this speed

def setup_tts_routes(tts_service):
    """Setup TTS routes with the provided TTS service"""
    router = APIRouter(prefix="/api/tts", tags=["tts"])

    @router.post("/spoken")
    async def spoken_text(request: Request):
        """RUFFS: texto para LEER en voz alta una respuesta (resumen hablado si
        trae tablas/código/listas o es larga). Solo para el audio: no se guarda
        en el chat ni se muestra."""
        from src.auth_helpers import require_user
        from src.spoken_summary import spoken_version
        owner = require_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        mode = body.get("mode") or "smart"
        if mode not in ("smart", "summary", "full"):
            mode = "smart"
        return await spoken_version(str(body.get("text") or ""), mode=mode, owner=owner or None)

    @router.get("/stats")
    async def get_tts_stats():
        """Get TTS service statistics"""
        try:
            stats = tts_service.get_stats()
            # Inject user-level settings into stats
            try:
                from src.settings import load_settings
                settings = load_settings()
                stats["auto_play"] = settings.get("tts_auto_play", False)
                stats["auto_play_mode"] = settings.get("tts_auto_play_mode", "smart")
                stats["wake_word"] = settings.get("tts_wake_word", "RUFFS")
                stats["wake_word_enabled"] = settings.get("tts_wake_word_enabled", False)
            except Exception:
                stats["auto_play"] = False
                stats["wake_word"] = "RUFFS"
                stats["wake_word_enabled"] = False
            return stats
        except Exception as e:
            logger.error(f"Failed to get TTS stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/synthesize")
    async def synthesize_speech(request: TTSRequest):
        """Synthesize speech from text"""
        try:
            # If provider override is given, bypass the global available check
            override_provider = request.provider
            if not override_provider and not tts_service.available:
                raise HTTPException(
                    status_code=503,
                    detail={"message": "TTS service not available"}
                )
            
            if request.format == "base64":
                audio_b64 = tts_service.synthesize_to_base64(request.text, provider=override_provider, voice=request.voice, speed=request.speed)
                if not audio_b64:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                return {"audio": audio_b64}
            
            else:  # audio format
                audio_data = tts_service.synthesize(
                    request.text,
                    provider=override_provider,
                    voice=request.voice,
                    speed=request.speed
                )
                if not audio_data:
                    raise HTTPException(
                        status_code=500,
                        detail={"message": "Synthesis failed"}
                    )
                
                # Detect format from magic bytes (MP3: ID3 tag or sync word ff e0+)
                is_mp3 = audio_data[:3] == b'ID3' or (len(audio_data) >= 2 and audio_data[0] == 0xff and (audio_data[1] & 0xe0) == 0xe0)
                mime = "audio/mpeg" if is_mp3 else "audio/wav"
                return Response(
                    content=audio_data,
                    media_type=mime,
                    headers={
                        "Content-Disposition": "inline; filename=speech.mp3" if "mpeg" in mime else "inline; filename=speech.wav"
                    }
                )
        
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Synthesis error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": f"Synthesis failed: {str(e)}"}
            )

    @router.post("/clear-cache")
    async def clear_tts_cache():
        """Clear TTS cache"""
        try:
            tts_service.clear_cache()
            return {"success": True, "message": "Cache cleared"}
        except Exception as e:
            logger.error(f"Failed to clear cache: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/voices")
    async def list_voices(provider: str = None):
        """List available voices for a TTS provider"""
        try:
            if provider == "edge_tts":
                voices = tts_service.list_edge_voices()
                return {"provider": "edge_tts", "voices": voices}
            elif provider == "elevenlabs":
                voices = tts_service.list_elevenlabs_voices()
                return {"provider": "elevenlabs", "voices": voices}
            else:
                # Return all providers
                result = {"edge_tts": tts_service.list_edge_voices()}
                if tts_service.elevenlabs_available:
                    result["elevenlabs"] = tts_service.list_elevenlabs_voices()
                return {"voices": result}
        except Exception as e:
            logger.error(f"Failed to list voices: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/settings/toggle")
    async def toggle_tts_setting(setting: str, value: bool):
        """Toggle a TTS boolean setting (auto_play, wake_word_enabled)"""
        try:
            from src.settings import load_settings, save_settings
            settings = load_settings()
            key_map = {"auto_play": "tts_auto_play", "wake_word_enabled": "tts_wake_word_enabled"}
            if setting in key_map:
                settings[key_map[setting]] = value
                save_settings(settings)
                return {"success": True, setting: value}
            raise HTTPException(status_code=400, detail=f"Unknown setting: {setting}")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to toggle TTS setting: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
