"""
Local speech-to-text using faster-whisper.

Replaces Chrome's Google-dependent Web Speech API. The browser captures mic
audio and streams 16kHz PCM here; we transcribe locally on this machine.
Proven accurate (see tests/test_whisper_roundtrip.py): 100% word accuracy,
reliably catches the "Jarvis" wake word, ~0.3s per utterance on CPU.
"""

import logging
import os

import numpy as np
from faster_whisper import WhisperModel

log = logging.getLogger("jarvis")

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base.en")
# device/compute: CPU int8 is fast and reliable everywhere. The RTX 5070 (Blackwell)
# CUDA support in ctranslate2 is not yet dependable, so default to CPU.
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE", "int8")

_model: WhisperModel | None = None


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        log.info(f"[stt] loading faster-whisper '{WHISPER_MODEL}' ({WHISPER_DEVICE}/{WHISPER_COMPUTE})...")
        _model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
        log.info("[stt] whisper ready")
    return _model


def transcribe_pcm(pcm: np.ndarray) -> str:
    """Transcribe 16kHz mono float32 PCM in [-1, 1]. Returns plain text."""
    if pcm is None or len(pcm) == 0:
        return ""
    # Skip near-silent audio (avoids whisper hallucinating on noise)
    rms = float(np.sqrt(np.mean(pcm ** 2)))
    if rms < 0.005:
        return ""
    model = get_model()
    segments, _ = model.transcribe(
        pcm, language="en", beam_size=1,
        vad_filter=True,  # built-in VAD trims leading/trailing silence
        condition_on_previous_text=False,  # each utterance independent
    )
    return " ".join(s.text for s in segments).strip()


def pcm_int16_bytes_to_float32(raw: bytes) -> np.ndarray:
    """Convert raw little-endian Int16 PCM bytes to float32 [-1, 1]."""
    if not raw:
        return np.array([], dtype=np.float32)
    ints = np.frombuffer(raw, dtype="<i2")
    return ints.astype(np.float32) / 32768.0
