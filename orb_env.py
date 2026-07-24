"""ORB_* → JARVIS_* environment aliasing.

Orb is the brand; the codebase's env vars grew up as JARVIS_*. Docs and
.env.example teach ORB_* — this shim maps any ORB_-prefixed variable onto its
JARVIS_ twin, so both generations of names work everywhere, forever. Per the
BACKEND_MIGRATION contract (2026-07-23): read the new name first, fall back to
legacy — so when both are set, ORB_* wins. Import AFTER .env is loaded.
"""
import os


def apply() -> None:
    for k, v in list(os.environ.items()):
        if k.startswith("ORB_"):
            os.environ["JARVIS_" + k[4:]] = v


apply()
