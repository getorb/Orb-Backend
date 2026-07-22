"""
Intent ground truth loader (`intents.yaml`).

The single source of truth for routing (see INTENT_ROUTING_PLAN.md). Everything
downstream reads from here so adding a capability is a config edit, not a code
change:
  - Layer 1 (bucket classifier) reads `buckets()` / `bucket_of()`.
  - Layer 2 (slot extractor) reads `slots_for()`.
  - The embedding matcher reads `exemplars()` (example phrases per intent).
  - The confidence/safety gate reads `has_side_effects()` (never fire a
    side-effects intent on a guessed classification — confirm first).

Pure + dependency-light (PyYAML, already installed). Loaded once and cached;
`validate()` checks structure so a malformed edit fails loudly in tests rather
than silently misrouting at runtime.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).parent / "intents.yaml"

VALID_BUCKETS = {
    "data", "communication", "media", "desktop",
    "research", "productivity", "conversation",
}
VALID_HANDLERS = {"registry", "router", "connector"}
VALID_SLOT_TYPES = {"string", "boolean", "integer", "enum"}


@lru_cache(maxsize=1)
def load() -> dict:
    """Parse intents.yaml once (cached). Returns the raw dict."""
    with _PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def intents() -> dict:
    """All intents, keyed by name."""
    return load().get("intents", {}) or {}


def names() -> list[str]:
    return sorted(intents().keys())


def get(name: str) -> dict | None:
    return intents().get(name)


def bucket_of(name: str) -> str | None:
    i = get(name)
    return i.get("bucket") if i else None


def buckets() -> dict[str, list[str]]:
    """bucket -> [intent names] (for Layer-1 triage)."""
    out: dict[str, list[str]] = {}
    for n, i in intents().items():
        out.setdefault(i.get("bucket", "?"), []).append(n)
    return out


def slots_for(name: str) -> dict:
    i = get(name)
    return (i or {}).get("slots") or {}


def required_slots(name: str) -> list[str]:
    return [s for s, meta in slots_for(name).items() if (meta or {}).get("required")]


def has_side_effects(name: str) -> bool:
    i = get(name)
    return bool(i and i.get("side_effects"))


def canonical_for(name: str) -> str | None:
    """A phrase the regex router parses into this intent with default slots, if
    the intent declares one."""
    i = get(name)
    return (i or {}).get("canonical")


def is_offline_routable(name: str) -> bool:
    """True if the semantic layer may fire this intent above the high-confidence
    threshold WITHOUT slot extraction: it must be read-only (no side effects) and
    declare a `canonical` form. Slot-bearing reads (no canonical) fall through to
    the Haiku rewrite, which extracts the slot from the utterance."""
    i = get(name)
    return bool(i and not i.get("side_effects") and i.get("canonical"))


def examples_for(name: str) -> list[str]:
    i = get(name)
    return list((i or {}).get("examples") or [])


def exemplars() -> dict[str, list[str]]:
    """intent name -> example phrases (the embedding exemplar set)."""
    return {n: examples_for(n) for n in names()}


def validate() -> list[str]:
    """Return a list of problems (empty = valid). Used by tests."""
    problems: list[str] = []
    data = load()
    its = data.get("intents")
    if not isinstance(its, dict) or not its:
        return ["no intents defined"]
    for n, i in its.items():
        if not isinstance(i, dict):
            problems.append(f"{n}: not a mapping")
            continue
        b = i.get("bucket")
        if b not in VALID_BUCKETS:
            problems.append(f"{n}: bad bucket {b!r}")
        if i.get("handler") not in VALID_HANDLERS:
            problems.append(f"{n}: bad handler {i.get('handler')!r}")
        if not i.get("tool"):
            problems.append(f"{n}: missing tool")
        if "side_effects" not in i:
            problems.append(f"{n}: missing side_effects flag")
        ex = i.get("examples")
        if not isinstance(ex, list) or not ex:
            problems.append(f"{n}: needs at least one example phrase")
        slots = i.get("slots") or {}
        if not isinstance(slots, dict):
            problems.append(f"{n}: slots must be a mapping")
            continue
        for sname, smeta in slots.items():
            if not isinstance(smeta, dict):
                problems.append(f"{n}.{sname}: slot must be a mapping")
                continue
            st = smeta.get("type")
            if st not in VALID_SLOT_TYPES:
                problems.append(f"{n}.{sname}: bad slot type {st!r}")
            if st == "enum" and not smeta.get("values"):
                problems.append(f"{n}.{sname}: enum slot needs 'values'")
    return problems
