"""Local neural TTS using Kokoro-ONNX (Apache-2.0, ~82M params).

Drop-in replacement for edge_tts in server_win.py. Falls back to edge-tts if
Kokoro is not installed so the backend degrades gracefully during the transition.

Setup (one-time, run in the venv):
    pip install kokoro-onnx
    python -c "from kokoro_onnx import Kokoro; k=Kokoro('kokoro-v0_19.onnx', 'voices.json'); print('ok')"

Kokoro model files (download once, ~310MB total):
    kokoro-v0_19.onnx     — https://github.com/thewh1teagle/kokoro-onnx/releases
    voices.json           — same release

Place both in the repo root (same dir as server_win.py).

Voice mappings (British English):
    JARVIS  → bf_emma or bm_lewis (British male, calm)
    ULTRON  → am_adam (American male, deeper)
    Fallback→ af_sky  (American female, clear)

Available voices in kokoro-v0_19: af_bella, af_heart, af_nicole, af_sarah, af_sky,
am_adam, am_michael, bf_emma, bf_isabella, bm_george, bm_lewis.
"""

import asyncio
import io
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_BASE = Path(__file__).parent.parent  # repo root
_MODEL_PATH = _BASE / "kokoro-v0_19.onnx"
_VOICES_PATH = _BASE / "voices.json"

# Map edge-tts voice names → Kokoro voice IDs
_VOICE_MAP = {
    "en-GB-RyanNeural":   "bm_george",   # JARVIS: British male
    "en-US-GuyNeural":    "am_adam",      # ULTRON: American male (deeper)
}
_FALLBACK_VOICE = "af_sky"

_kokoro = None
_kokoro_available: bool | None = None  # None = not yet checked


def _load_kokoro():
    global _kokoro, _kokoro_available
    if _kokoro_available is not None:
        return _kokoro_available
    if not _MODEL_PATH.exists() or not _VOICES_PATH.exists():
        log.info("[tts_local] Kokoro model files not found — falling back to edge-tts")
        _kokoro_available = False
        return False
    try:
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(str(_MODEL_PATH), str(_VOICES_PATH))
        log.info("[tts_local] Kokoro TTS loaded — using local neural voice")
        _kokoro_available = True
        return True
    except ImportError:
        log.info("[tts_local] kokoro-onnx not installed — falling back to edge-tts")
        _kokoro_available = False
        return False
    except Exception as e:
        log.warning(f"[tts_local] Kokoro load failed ({e}) — falling back to edge-tts")
        _kokoro_available = False
        return False


async def synthesize(text: str, tts_voice: str = "en-GB-RyanNeural") -> bytes:
    """Synthesize speech. Uses Kokoro when available, edge-tts otherwise.
    Returns raw MP3 bytes compatible with the existing {type:'audio'} WS message.
    """
    if _load_kokoro():
        return await _synthesize_kokoro(text, tts_voice)
    return await _synthesize_edge(text, tts_voice)


async def _synthesize_kokoro(text: str, tts_voice: str) -> bytes:
    voice = _VOICE_MAP.get(tts_voice, _FALLBACK_VOICE)
    loop = asyncio.get_running_loop()

    def _run():
        import soundfile as sf
        samples, sample_rate = _kokoro.create(text, voice=voice, speed=1.0, lang="en-us")
        buf = io.BytesIO()
        sf.write(buf, samples, sample_rate, format="mp3")
        return buf.getvalue()

    try:
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        log.warning(f"[tts_local] Kokoro synthesis failed ({e}) — falling back to edge-tts")
        return await _synthesize_edge(text, tts_voice)


async def _synthesize_edge(text: str, tts_voice: str) -> bytes:
    import edge_tts
    communicate = edge_tts.Communicate(text, tts_voice)
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    return buf.getvalue()


def is_local_available() -> bool:
    """True if Kokoro is installed and model files are present."""
    return _load_kokoro()
