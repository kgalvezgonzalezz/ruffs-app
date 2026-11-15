# src/tts_service.py
"""Multi-provider TTS service — dispatches to local Kokoro, ElevenLabs API, OpenAI-compatible API, or browser."""

import io
import wave
import logging
import hashlib
import httpx
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def _safe_speed(value, default: float = 1.0) -> float:
    """Parse the stored tts_speed defensively. The settings layer tolerates
    corrupt/agent-written config, so a non-numeric or empty value (e.g. an agent
    setting "speech speed" = "fast", or a hand-edited settings.json) must not
    crash synthesis or the stats endpoint with a ValueError."""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return default
    return speed if speed > 0 else default


class TTSService:
    """Multi-provider TTS service.

    Reads provider config from data/settings.json on each call.
    Providers:
      "disabled"        — no TTS
      "browser"         — client-side Web Speech API (no server synthesis)
      "local"           — Kokoro-82M on GPU
      "elevenlabs"      — ElevenLabs REST API (requires ELEVENLABS_API_KEY)
      "endpoint:<id>"   — OpenAI-compatible /audio/speech via ModelEndpoint
    """

    def __init__(self, cache_dir: str = "data/tts_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._kokoro = None  # lazy-init
        self._edge_tts = None  # lazy-init

    # ── Settings ──

    def _load_settings(self) -> dict:
        from src.settings import load_settings
        saved = load_settings()
        return {
            "tts_enabled": saved.get("tts_enabled", True),
            "tts_provider": saved.get("tts_provider", "disabled"),
            "tts_model": saved.get("tts_model", "eleven_flash_v2_5"),
            "tts_voice": saved.get("tts_voice", "alloy"),
            "tts_speed": saved.get("tts_speed", "1"),
            "tts_auto_play": saved.get("tts_auto_play", False),
            "tts_wake_word": saved.get("tts_wake_word", "RUFFS"),
            "tts_wake_word_enabled": saved.get("tts_wake_word_enabled", False),
        }

    @property
    def available(self) -> bool:
        settings = self._load_settings()
        if settings.get("tts_enabled") is False:
            return False
        provider = settings["tts_provider"]
        if provider == "disabled":
            return False
        if provider == "browser":
            return True  # handled client-side
        if provider == "edge_tts":
            edge = self._get_edge_tts()
            return edge is not None and edge.available
        if provider == "local":
            kokoro = self._get_kokoro()
            return kokoro is not None and kokoro.available
        if provider == "elevenlabs":
            import os
            return bool(os.environ.get("ELEVENLABS_API_KEY"))
        if provider.startswith("endpoint:"):
            return True  # assume reachable; errors surface at synthesis time
        return False

    # ── Cache ──

    def _cache_key(self, text: str, provider: str, model: str, voice: str, speed: float = 1.0) -> str:
        raw = f"{provider}|{model}|{voice}|{speed}|{text}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _get_cached(self, key: str) -> Optional[bytes]:
        for ext in (".mp3", ".wav"):
            path = self.cache_dir / f"{key}{ext}"
            if path.exists():
                return path.read_bytes()
        return None

    def _put_cache(self, key: str, data: bytes):
        ext = ".mp3" if (len(data) >= 3 and (data[:3] == b'ID3' or (data[0] == 0xff and (data[1] & 0xe0) == 0xe0))) else ".wav"
        (self.cache_dir / f"{key}{ext}").write_bytes(data)

    def clear_cache(self):
        count = 0
        for f in self.cache_dir.glob("*.*"):
            f.unlink()
            count += 1
        logger.info(f"Cleared {count} cached TTS files")

    # ── Kokoro (local) ──

    def _get_kokoro(self):
        if self._kokoro is None:
            self._kokoro = _KokoroPipeline()
        return self._kokoro

    def _get_edge_tts(self):
        if self._edge_tts is None:
            self._edge_tts = EdgeTTSProvider()
        return self._edge_tts

    def list_edge_voices(self):
        """List available Edge TTS voices."""
        provider = self._get_edge_tts()
        if not provider or not provider.available:
            return []
        try:
            import asyncio
            import concurrent.futures
            async def _list():
                import edge_tts
                return await edge_tts.VoicesManager.create()
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, _list())
                manager = future.result(timeout=15)
            voices = []
            for v in manager.voices:
                voices.append({
                    "id": v["ShortName"],
                    "ShortName": v["ShortName"],
                    "FriendlyName": v.get("FriendlyName", v["ShortName"]),
                    "Locale": v.get("Locale", ""),
                    "Gender": v.get("Gender", ""),
                })
            return voices
        except Exception as e:
            logger.error(f"Failed to list Edge TTS voices: {e}")
            return []

    # ── API endpoint ──

    def _synthesize_api(self, text: str, endpoint_id: str, model: str, voice: str, speed: float = 1.0) -> Optional[bytes]:
        from src.database import SessionLocal, ModelEndpoint

        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
            if not ep:
                logger.error(f"TTS endpoint {endpoint_id} not found")
                return None
            base_url = ep.base_url.rstrip("/")
            api_key = ep.api_key
        finally:
            db.close()

        url = base_url + "/audio/speech"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "speed": speed,
        }

        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=60)
            r.raise_for_status()
            logger.info(f"API TTS: {len(r.content)} bytes from {base_url}")
            return r.content
        except Exception as e:
            logger.error(f"API TTS synthesis failed: {e}")
            return None

    # ── ElevenLabs ──

    def _synthesize_elevenlabs(self, text: str, voice: str, speed: float = 1.0) -> Optional[bytes]:
        """Synthesize speech via ElevenLabs REST API."""
        import os
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            logger.error("ELEVENLABS_API_KEY not set")
            return None

        voice_id = voice if voice and voice != "alloy" else "Xb7hH8MSUJpSbSDYk0k2"
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        }
        payload = {
            "text": text,
            "model_id": "eleven_flash_v2_5",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                            },
        }
        try:
            r = httpx.post(url, json=payload, headers=headers, timeout=60)
            r.raise_for_status()
            logger.info(f"ElevenLabs TTS: {len(r.content)} bytes")
            return r.content
        except Exception as e:
            logger.error(f"ElevenLabs TTS failed: {e}")
            return None

    # ── Public interface ──

    def synthesize(self, text: str, use_cache: bool = True,
                  provider: Optional[str] = None,
                  voice: Optional[str] = None,
                  speed: Optional[float] = None) -> Optional[bytes]:
        settings = self._load_settings()
        if settings.get("tts_enabled") is False and provider is None:
            return None
        # Allow caller to override provider/voice/speed (e.g. Preview button)
        _provider = provider or settings["tts_provider"]
        model = settings["tts_model"]
        _voice = voice or settings["tts_voice"]
        _speed = speed if speed is not None else _safe_speed(settings.get("tts_speed", "1"))

        if _provider in ("disabled", "browser"):
            return None

        if len(text) > 5000:
            text = text[:5000]

        if not text:
            return None

        if use_cache:
            key = self._cache_key(text, _provider, model, _voice, _speed)
            cached = self._get_cached(key)
            if cached:
                logger.info(f"TTS cache hit ({len(text)} chars)")
                return cached

        audio_data = None

        if _provider == "local":
            kokoro = self._get_kokoro()
            if kokoro and kokoro.available:
                audio_data = kokoro.synthesize_raw(text, _voice)
            else:
                logger.warning("Kokoro TTS not available")
                return None
        elif _provider == "edge_tts":
            edge = self._get_edge_tts()
            if edge and edge.available:
                audio_data = edge.synthesize(text, _voice, _speed)
            else:
                logger.warning("Edge TTS not available")
                return None
        elif _provider == "elevenlabs":
            audio_data = self._synthesize_elevenlabs(text, _voice, _speed)
        elif _provider.startswith("endpoint:"):
            endpoint_id = _provider.split(":", 1)[1]
            audio_data = self._synthesize_api(text, endpoint_id, model, _voice, _speed)
        else:
            logger.error(f"Unknown TTS provider: {_provider}")
            return None

        if audio_data and use_cache:
            key = self._cache_key(text, _provider, model, _voice, _speed)
            self._put_cache(key, audio_data)

        return audio_data

    def synthesize_to_base64(self, text: str,
                               provider: Optional[str] = None,
                               voice: Optional[str] = None,
                               speed: Optional[float] = None) -> Optional[str]:
        import base64
        audio = self.synthesize(text, provider=provider, voice=voice, speed=speed)
        if audio:
            return base64.b64encode(audio).decode("utf-8")
        return None

    def set_voice(self, voice: str):
        """Legacy no-op — voice is now managed via admin settings."""

    def get_stats(self) -> Dict[str, Any]:
        settings = self._load_settings()
        provider = settings["tts_provider"]
        tts_enabled = settings.get("tts_enabled", True)

        cache_files = list(self.cache_dir.glob("*.wav")) + list(self.cache_dir.glob("*.mp3"))
        cache_size = sum(f.stat().st_size for f in cache_files)

        is_available = self.available and tts_enabled
        stats = {
            "available": is_available,
            "ready": is_available,
            "provider": provider,
            "model": settings["tts_model"],
            "voice": settings["tts_voice"],
            "speed": _safe_speed(settings.get("tts_speed", "1")),
            "cache_entries": len(cache_files),
            "cache_size_mb": round(cache_size / (1024 * 1024), 2),
        }

        # Expose auto-play & wake-word settings to frontend
        stats["auto_play"] = settings.get("tts_auto_play", False)
        stats["wake_word"] = settings.get("tts_wake_word", "RUFFS")
        stats["wake_word_enabled"] = settings.get("tts_wake_word_enabled", False)

        if provider == "local":
            kokoro = self._get_kokoro()
            stats["model"] = "Kokoro-82M (GPU)" if (kokoro and kokoro.available) else "Kokoro (not loaded)"
        elif provider == "edge_tts":
            stats["model"] = "Microsoft Edge TTS (free)"
        elif provider == "elevenlabs":
            stats["model"] = "ElevenLabs (API)"
        elif provider == "browser":
            stats["model"] = "Browser (Web Speech API)"
        elif provider.startswith("endpoint:"):
            stats["endpoint_id"] = provider.split(":", 1)[1]

        return stats


class EdgeTTSProvider:
    """Free, unlimited TTS via Microsoft Edge speech service (edge-tts).
    No API key required. 100+ languages. Returns MP3 bytes."""

    def __init__(self):
        self._available = False
        self._init()

    def _init(self):
        try:
            import edge_tts
            self._available = True
            logger.info('Edge TTS (Microsoft) provider available - free & unlimited')
        except ImportError:
            logger.warning('Edge TTS not available: pip install edge-tts')
        except Exception as e:
            logger.error(f'Edge TTS init failed: {e}')

    @property
    def available(self):
        return self._available

    async def synthesize_async(self, text: str, voice: str = 'es-CL-CatalinaNeural', speed: float = 1.0):
        if not self._available:
            return None
        try:
            import edge_tts
            rate_str = f'{int((speed - 1.0) * 100):+d}%' if speed != 1.0 else '+0%'
            communicate = edge_tts.Communicate(text, voice, rate=rate_str)
            audio_chunks = []
            async for chunk in communicate.stream():
                if chunk['type'] == 'audio':
                    audio_chunks.append(chunk['data'])
            if not audio_chunks:
                return None
            return b''.join(audio_chunks)
        except Exception as e:
            logger.error(f'Edge TTS synthesis failed: {e}')
            return None

    def synthesize(self, text: str, voice: str = 'es-CL-CatalinaNeural', speed: float = 1.0):
        import asyncio, threading, time
        result = {'data': None, 'error': None, 'done': False}

        async def _run():
            try:
                result['data'] = await self.synthesize_async(text, voice, speed)
            except Exception as ex:
                result['error'] = ex
            finally:
                result['done'] = True

        def _thread_run():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                new_loop.run_until_complete(_run())
            finally:
                new_loop.close()

        t = threading.Thread(target=_thread_run, daemon=True)
        t.start()
        t.join(timeout=60)

        if result['error']:
            logger.error(f'Edge TTS synthesis error: {result["error"]}')
            return None
        return result['data']
class _KokoroPipeline:
    """Encapsulates the Kokoro-82M local GPU pipeline."""

    def __init__(self):
        self.pipeline = None
        self.available = False
        self.device = None
        self._init()

    def _init(self):
        try:
            import torch
            from kokoro import KPipeline

            if not torch.cuda.is_available():
                logger.warning("CUDA not available for Kokoro TTS")
                return

            self.device = torch.device("cuda:0")
            with torch.cuda.device(0):
                self.pipeline = KPipeline(lang_code="a")
                if hasattr(self.pipeline, "model"):
                    self.pipeline.model = self.pipeline.model.to(self.device)
            self.available = True
            logger.info("Kokoro-82M TTS pipeline loaded")
        except ImportError as e:
            logger.warning(f"Kokoro TTS not available: {e}")
            logger.warning("Install with: pip install kokoro soundfile")
        except Exception as e:
            logger.error(f"Kokoro init failed: {e}", exc_info=True)

    def synthesize_raw(self, text: str, voice: str = "af_heart") -> Optional[bytes]:
        if not self.available:
            return None
        try:
            import torch
            import numpy as np

            with torch.cuda.device(self.device):
                chunks = []
                for _, _, audio in self.pipeline(text, voice=voice):
                    chunks.append(audio)

            if not chunks:
                return None

            full = np.concatenate(chunks)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes((full * 32767).astype(np.int16).tobytes())
            return buf.getvalue()
        except Exception as e:
            logger.error(f"Kokoro synthesis failed: {e}", exc_info=True)
            return None


# Module-level singleton
_tts_service = None

def get_tts_service() -> TTSService:
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service
