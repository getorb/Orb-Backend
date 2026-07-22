"""
Semantic intent router — Layer 1 (paraphrase matching via local embeddings).

Sits BETWEEN the deterministic regex router (`intent_router`, Layer 0) and the
Haiku rewrite fallback. Catches paraphrases the regex misses ("should I bring a
jacket tomorrow" -> weather) using cosine similarity against the example phrases
in `intents.yaml` (the single source of truth, via `intent_config`). Embeddings
are local + offline (`nomic-embed-text` in Ollama), so this costs nothing and
needs no cloud.

Decision policy (the chosen hybrid — reads bold, actions confirm):
  - read-only + OFFLINE-ROUTABLE (has a `canonical`) + score >= HIGH
        -> "route": fire it (re-feed the canonical to intent_router so it reuses
           every existing handler).
  - everything else (side-effects intents, slot-bearing reads, conversation,
        or low confidence) -> None: fall through to the existing path (Haiku
        rewrite handles slot extraction; actions reach their confirm-capable
        handlers; smalltalk becomes free conversation).
  So actions are NEVER auto-fired here — that's "actions confirm" in practice.
  The interactive confirm card + slot extraction for actions are the next layers
  (see INTENT_ROUTING_PLAN.md).

OFF BY DEFAULT — enable with env `JARVIS_SEMANTIC_ROUTER=1`. The embedder is
injectable (`set_embedder`) so the cosine logic is unit-tested offline with a
deterministic fake (no Ollama).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

import intent_config

# Cosine threshold for a confident match. Conservative by default: rather fall
# through to the LLM than route to the wrong tool. 0.70 is data-backed — the
# highest-scoring *offline-routable* negative seen in eval_semantic_router was
# smalltalk classifying as get_news at 0.673, so 0.70 sits safely above it while
# catching near-miss positives (see tests/eval_semantic_router.py). Side-effects
# intents are gated separately (never auto-fire), so they don't constrain this.
THRESHOLD = float(os.getenv("JARVIS_SEMANTIC_THRESHOLD", "0.70"))

_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
_EMBED_MODEL = os.getenv("JARVIS_EMBED_MODEL", "nomic-embed-text")
_CACHE_PATH = Path(__file__).parent / "data" / "semantic_cache.json"


@dataclass
class Match:
    intent: str
    score: float


@dataclass
class Decision:
    action: str            # "route" (only value today; "confirm"/"ask" later)
    intent: str
    canonical: str
    score: float
    bucket: str | None = None


def enabled() -> bool:
    return os.getenv("JARVIS_SEMANTIC_ROUTER", "").lower() in ("1", "true", "yes", "on")


async def _ollama_embed(text: str) -> list[float]:
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{_OLLAMA_URL}/api/embeddings",
            json={"model": _EMBED_MODEL, "prompt": text},
        )
        r.raise_for_status()
        return r.json()["embedding"]


# Swappable embedder (async str -> list[float]). Default = Ollama; tests inject a
# deterministic fake. A custom embedder disables the disk cache (its vectors
# aren't the real model's).
_embedder = _ollama_embed
_use_disk_cache = True


def set_embedder(fn) -> None:
    global _embedder, _use_disk_cache, _EXEMPLAR_VECS
    _embedder = fn
    _use_disk_cache = False
    _EXEMPLAR_VECS = None  # invalidate when the embedder changes


# Cache of exemplar vectors: list of (intent_name, vector). Built lazily.
_EXEMPLAR_VECS: list[tuple[str, list[float]]] | None = None


def _exemplar_signature() -> str:
    """Hash of the exemplar set + model — the disk-cache key. Changes when
    intents.yaml's examples change, forcing a rebuild."""
    payload = json.dumps(intent_config.exemplars(), sort_keys=True) + "|" + _EMBED_MODEL
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_disk_cache(sig: str) -> list[tuple[str, list[float]]] | None:
    if not _use_disk_cache:
        return None
    try:
        blob = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if blob.get("sig") == sig:
            return [(n, v) for n, v in blob.get("vecs", [])]
    except Exception:
        pass
    return None


def _save_disk_cache(sig: str, vecs: list[tuple[str, list[float]]]) -> None:
    if not _use_disk_cache:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps({"sig": sig, "vecs": vecs}), encoding="utf-8")
    except Exception:
        pass


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def _ensure_exemplar_vecs() -> list[tuple[str, list[float]]]:
    global _EXEMPLAR_VECS
    if _EXEMPLAR_VECS is not None:
        return _EXEMPLAR_VECS
    sig = _exemplar_signature()
    cached = _load_disk_cache(sig)
    if cached:
        _EXEMPLAR_VECS = cached
        return cached
    vecs: list[tuple[str, list[float]]] = []
    for intent, phrases in intent_config.exemplars().items():
        for p in phrases:
            try:
                vecs.append((intent, await _embedder(p)))
            except Exception:
                continue
    _EXEMPLAR_VECS = vecs
    _save_disk_cache(sig, vecs)
    return vecs


async def classify(utterance: str) -> Match | None:
    """Best-matching intent for `utterance` if cosine >= THRESHOLD, else None.
    Never raises — embedding failures return None."""
    if not utterance or not utterance.strip():
        return None
    try:
        uvec = await _embedder(utterance)
    except Exception:
        return None
    vecs = await _ensure_exemplar_vecs()
    best: Match | None = None
    for intent, ev in vecs:
        s = _cosine(uvec, ev)
        if best is None or s > best.score:
            best = Match(intent=intent, score=s)
    if best and best.score >= THRESHOLD:
        return best
    return None


async def decide(utterance: str) -> Decision | None:
    """Apply the hybrid policy. Returns a "route" Decision only for a confident,
    read-only, offline-routable intent; otherwise None (caller falls through)."""
    m = await classify(utterance)
    if not m:
        return None
    if intent_config.is_offline_routable(m.intent):
        canonical = intent_config.canonical_for(m.intent)
        if canonical:
            return Decision(action="route", intent=m.intent, canonical=canonical,
                            score=m.score, bucket=intent_config.bucket_of(m.intent))
    return None
