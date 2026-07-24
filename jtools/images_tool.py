"""Image search tool — pulls real image URLs from the web, NO API key.

Like Google Images, but cutting Google out: we scrape Bing's image index (which
is scrape-tolerant and keyless, unlike Google) and keep, for each result, BOTH
the real image URL (hosted on the original site) AND its source page URL — so
tapping an image in the HUD opens the actual website it came from.

Sources, in order:
  1. Bing image search (HTML scrape) — broad coverage + source links. Primary.
  2. Wikipedia media-list — accurate canonical photos of known people/places.
  3. Openverse — free/CC image search.
(Bing can break if they change their HTML; 2 & 3 are the fallback.)

Returns a list of {"img": <image url>, "src": <source page url>} dicts.
"""

import asyncio
import html
import json
import logging
import re

import httpx

from tool_registry import tool
from jtools.cache_util import ttl_cache

log = logging.getLogger("orb.tools.images")

import random

# Rotate UA so a single fingerprint isn't rate-limited as fast.
_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
]


def _ua() -> str:
    return random.choice(_UAS)


# Wikimedia enforces a UA policy — must identify the bot + a contact.
_WIKI_UA = "Orb-Backend/0.2 (https://github.com/getorb/Orb-Backend)"

_IUSC_RE = re.compile(r'class="iusc"[^>]*\sm="([^"]+)"')

# Backstop in case SafeSearch ever leaks an explicit result.
_ADULT_RE = re.compile(
    r"(porn|xvideos|xnxx|pornhub|xhamster|redtube|youporn|onlyfans|rule34|"
    r"nsfw|hentai|nude|nudes|naked|escort|camgirl|chaturbate|brazzers|"
    r"sex\.com|adult|xxx)", re.IGNORECASE)


def _is_adult(*urls: str) -> bool:
    return any(_ADULT_RE.search(u or "") for u in urls)


def _clean_query(q: str) -> str:
    """Strip qualifier modifiers that break a lookup. 'Millie Bobby Brown that
    are recent' -> 'Millie Bobby Brown'. Conservative — only clearly-temporal
    qualifiers (never 'new'/'old', which can be part of a name like 'New York')."""
    q = (q or "").strip()
    q = re.sub(r"\s+that\s+(?:are|is|were|was)\b.*$", "", q, flags=re.I)
    q = re.sub(r"\b(?:most\s+)?(?:recent(?:ly)?|latest|newest|current|today'?s)\b", "", q, flags=re.I)
    q = re.sub(r"\b(?:from|in)\s+\d{4}\b", "", q, flags=re.I)
    q = re.sub(r"\s{2,}", " ", q).strip(" ,.-")
    return q


async def _bing_once(client: httpx.AsyncClient, query: str, count: int) -> list[dict]:
    out: list[dict] = []
    try:
        r = await client.get(
            "https://www.bing.com/images/search",
            params={"q": query, "form": "HDRSC2", "first": "1", "adlt": "moderate",
                    "safeSearch": "Moderate"},
            headers={"User-Agent": _ua(),
                     "Cookie": "SRCHHPGUSR=ADLT=DEMOTE; ADLT=DEMOTE"})
        if r.status_code != 200:
            return out
        seen = set()
        for raw in _IUSC_RE.findall(r.text):
            try:
                d = json.loads(html.unescape(raw))
            except Exception:
                continue
            img = d.get("murl") or ""
            src = d.get("purl") or img
            if img.startswith("http") and img not in seen and not _is_adult(img, src):
                seen.add(img)
                out.append({"img": img, "src": src})
            if len(out) >= count:
                break
    except Exception as e:
        log.warning(f"[images] bing failed: {e}")
    return out


async def _bing_images(client: httpx.AsyncClient, query: str, count: int) -> list[dict]:
    """Scrape Bing images — keyless, real source links, MODERATE SafeSearch (no
    nudity, but cleavage/attractive is fine) + adult-domain backstop. Rotates UA
    and retries once (new UA) on an empty result to ride out throttling."""
    out = await _bing_once(client, query, count)
    # Retry a couple times (new UA, backoff) on a thin/empty result — Bing
    # throttles bursts, and good Bing results beat garbage from CC fallbacks.
    for _ in range(2):
        if len(out) >= 3:
            break
        await asyncio.sleep(0.5)
        out = await _bing_once(client, query, count)
    return out


async def _wiki_resolve_title(client: httpx.AsyncClient, q: str) -> str | None:
    """Resolve a fuzzy query to the best Wikipedia page title via opensearch."""
    try:
        r = await client.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "opensearch", "search": q, "limit": 1,
                    "namespace": 0, "format": "json"},
            headers={"User-Agent": _WIKI_UA})
        if r.status_code == 200:
            data = r.json()
            titles = data[1] if len(data) > 1 else []
            if titles:
                return titles[0]
    except Exception as e:
        log.warning(f"[images] wiki opensearch failed: {e}")
    return None


async def _wikipedia_images(client: httpx.AsyncClient, query: str, count: int) -> list[dict]:
    """Canonical photos off a Wikipedia page (accurate for people/places).
    Source link is the article itself."""
    out: list[dict] = []
    resolved = await _wiki_resolve_title(client, query)
    if not resolved:
        return out
    title = resolved.strip().replace(" ", "_")
    src = f"https://en.wikipedia.org/wiki/{title}"
    try:
        r = await client.get(
            f"https://en.wikipedia.org/api/rest_v1/page/media-list/{title}",
            headers={"User-Agent": _WIKI_UA})
        if r.status_code != 200:
            return out
        for item in r.json().get("items", []):
            if item.get("type") != "image":
                continue
            srcset = item.get("srcset") or []
            if not srcset:
                continue
            img = srcset[0].get("src", "")
            if img.startswith("//"):
                img = "https:" + img
            if img and any(img.lower().endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp")):
                out.append({"img": img, "src": src})
            if len(out) >= count:
                break
    except Exception as e:
        log.warning(f"[images] wikipedia media-list failed: {e}")
    return out


async def _openverse_images(client: httpx.AsyncClient, query: str, count: int) -> list[dict]:
    """Free/CC image search. Source link is the original hosting page."""
    out: list[dict] = []
    try:
        r = await client.get(
            "https://api.openverse.org/v1/images/",
            params={"q": query, "page_size": count},
            headers={"User-Agent": _WIKI_UA})
        if r.status_code == 200:
            for it in r.json().get("results", []):
                img = it.get("thumbnail") or it.get("url")
                if img and img.startswith("http"):
                    out.append({"img": img,
                                "src": it.get("foreign_landing_url") or it.get("url") or img})
                if len(out) >= count:
                    break
    except Exception as e:
        log.warning(f"[images] openverse failed: {e}")
    return out


async def _commons_images(client: httpx.AsyncClient, query: str, count: int) -> list[dict]:
    """Wikimedia Commons image search — a real, generous, no-throttle API (vs
    scraping a search engine). Good for places/landmarks/topics/notable subjects."""
    out: list[dict] = []
    try:
        r = await client.get(
            "https://commons.wikimedia.org/w/api.php",
            params={"action": "query", "generator": "search", "gsrsearch": query,
                    "gsrnamespace": 6, "gsrlimit": count, "prop": "imageinfo",
                    "iiprop": "url", "format": "json"},
            headers={"User-Agent": _WIKI_UA})
        if r.status_code != 200:
            return out
        pages = (r.json().get("query", {}) or {}).get("pages", {}) or {}
        for p in pages.values():
            ii = (p.get("imageinfo") or [{}])[0]
            url = ii.get("url", "")
            if url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) and not _is_adult(url):
                out.append({"img": url, "src": ii.get("descriptionurl") or url})
            if len(out) >= count:
                break
    except Exception as e:
        log.warning(f"[images] commons failed: {e}")
    return out


@ttl_cache(600)  # same query re-asked/re-triggered by a follow-up shouldn't re-scrape
async def search_images(query: str, count: int = 12) -> list[dict]:
    """Return up to `count` {"img", "src"} results for `query`. Bing first (broad
    coverage + source links), then Wikipedia / Openverse as fallback. [] if none."""
    query = (query or "").strip()
    if not query:
        return []
    count = max(1, min(int(count or 12), 30))
    cleaned = _clean_query(query) or query
    results: list[dict] = []
    seen: set[str] = set()

    def _add(items):
        for it in items:
            if it["img"] not in seen:
                seen.add(it["img"])
                results.append(it)

    # PRIMARY: SearXNG (self-hosted metasearch — accurate, no single-engine
    # throttle). If it returns enough, we're done. Else fall to the scrapers.
    try:
        from jtools import searxng_tool
        _add(await searxng_tool.searx_images(cleaned, count))
        if len(results) >= 3:
            return results[:count]
    except Exception as e:
        log.warning(f"[images] searxng primary failed: {e}")

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        # Bing ranks by relevance/popularity (like Google) — so for 'Addison Rae'
        # the famous TikToker comes first. TRUST that ranking: only fall back to
        # Wikipedia/Openverse if Bing essentially failed, because topping up a
        # good Bing result with other sources can inject a DIFFERENT same-named
        # person and pollute relevance.
        _add(await _bing_images(client, cleaned, count))
        # Fallbacks ONLY when Bing essentially failed (throttled/thin) — so a good
        # Bing result isn't polluted by a same-named person. Chain through several
        # keyless sources for resilience: Wikipedia page photos, Wikimedia Commons
        # (a real no-throttle API), then Openverse (CC).
        if len(results) < 3:
            _add(await _wikipedia_images(client, cleaned, count - len(results)))
        if len(results) < 3:
            _add(await _commons_images(client, cleaned, count - len(results)))
        if len(results) < 3:
            _add(await _openverse_images(client, cleaned, count - len(results)))
    return results[:count]


@tool(
    name="search_images",
    description=(
        "Find real photos of a person, place, or object on the web. Use when the "
        "user asks to SEE something — 'show me a picture of X', 'what does X look "
        "like', 'pictures of X'. Returns image results with source links."
    ),
    parameters={
        "query": {"type": "string", "description": "What to find pictures of."},
        "count": {"type": "integer", "description": "How many images (1-30, default 12)."},
    },
    required=["query"],
)
async def search_images_tool(query: str, count: int = 12) -> str:
    results = await search_images(query, count)
    if not results:
        return f"I couldn't find any pictures of {query}."
    return f"Found {len(results)} images of {query}."
