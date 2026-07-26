"""SearXNG-backed search — the PRIMARY source for both images and web info.

Self-hosted metasearch at localhost:8888 (Docker container `orb-searxng`).
It aggregates many engines (Google/Bing/Brave/DDG/…), so no single provider
throttles or locks us out — fixing the inaccurate/empty results we got from
scraping Bing directly. Keyless. SafeSearch is moderate by default (block
explicit, keep the rest). The legacy scrapers remain as a fallback if SearXNG
is down.
"""

import logging

import httpx

log = logging.getLogger("orb.tools.searxng")

_SEARX = "http://localhost:8888/search"
_TIMEOUT = 15.0


async def searx_images(query: str, count: int = 12, safesearch: int = 1) -> list[dict]:
    """Image results as {"img", "src"} dicts. safesearch: 0 off / 1 moderate / 2 strict."""
    out: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(_SEARX, params={"q": query, "format": "json",
                                            "categories": "images", "safesearch": safesearch})
            if r.status_code == 200:
                seen = set()
                for it in r.json().get("results", []):
                    img = it.get("img_src") or ""
                    if img.startswith("http") and img not in seen:
                        seen.add(img)
                        out.append({"img": img, "src": it.get("url") or img})
                    if len(out) >= count:
                        break
    except Exception as e:
        log.warning(f"[searxng] images failed (is the container up?): {e}")
    return out


async def searx_web(query: str, count: int = 6, safesearch: int = 1) -> list[str]:
    """Web text snippets (for grounding research). Returns labelled lines; leads
    with any direct answer/infobox, then result snippets."""
    out: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(_SEARX, params={"q": query, "format": "json", "safesearch": safesearch})
            if r.status_code == 200:
                d = r.json()
                for a in (d.get("answers") or []):
                    if isinstance(a, str) and a.strip():
                        out.append(a.strip())
                for ib in (d.get("infoboxes") or []):
                    c0 = (ib.get("content") or "").strip()
                    if c0:
                        out.append(c0)
                for it in (d.get("results") or []):
                    sn = (it.get("content") or "").strip()
                    if len(sn) > 30:
                        out.append(sn)
                    if len(out) >= count:
                        break
    except Exception as e:
        log.warning(f"[searxng] web failed: {e}")
    return out[:count]


async def available() -> bool:
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(_SEARX, params={"q": "ping", "format": "json"})
            return r.status_code == 200
    except Exception:
        return False
