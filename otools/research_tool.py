"""Live research sources — keyless. Gathers CURRENT material about a subject so
the brain synthesizes from real, up-to-date sources instead of stale training
memory (which said Elon Musk was worth $250B). Sources:
  1. DuckDuckGo HTML *text* search snippets (current web facts; the text
     endpoint works even though the image endpoint is key-walled).
  2. Wikipedia REST summary extract (resolved via opensearch).

Returns a single labelled text blob to feed the synthesis prompt.
"""

import html
import logging
import re

import httpx

log = logging.getLogger("orb.tools.research")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_WIKI_UA = "Orb-Backend/0.2 (https://github.com/getorb/Orb-Backend)"
_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", t))).strip()


async def _ddg_snippets(client: httpx.AsyncClient, query: str, n: int) -> list[str]:
    try:
        # default (moderate) SafeSearch — filters explicit, doesn't over-filter
        # legitimate topics. (Text snippets, so image nudity isn't the concern.)
        r = await client.get("https://html.duckduckgo.com/html/", params={"q": query})
        if r.status_code == 200:
            out = []
            for m in _SNIPPET_RE.findall(r.text):
                s = _clean(m)
                if len(s) > 30:
                    out.append(s)
                if len(out) >= n:
                    break
            return out
    except Exception as e:
        log.warning(f"[research] ddg text failed: {e}")
    return []


async def _wikipedia_extract(client: httpx.AsyncClient, query: str) -> str:
    try:
        r = await client.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "opensearch", "search": query, "limit": 1,
                    "namespace": 0, "format": "json"},
            headers={"User-Agent": _WIKI_UA})
        title = None
        if r.status_code == 200:
            data = r.json()
            if len(data) > 1 and data[1]:
                title = data[1][0]
        if not title:
            title = query
        r2 = await client.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}",
            headers={"User-Agent": _WIKI_UA})
        if r2.status_code == 200:
            return _clean(r2.json().get("extract", ""))
    except Exception as e:
        log.warning(f"[research] wikipedia failed: {e}")
    return ""


async def gather(subject: str, max_snippets: int = 5) -> str:
    """Return a labelled blob of CURRENT sources about `subject`, or "" if none."""
    subject = (subject or "").strip()
    if not subject:
        return ""
    parts: list[str] = []
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as client:
        extract = await _wikipedia_extract(client, subject)
        if extract:
            parts.append(f"[Wikipedia] {extract}")
        # PRIMARY web snippets: SearXNG (multi-engine, current). Fall back to the
        # DuckDuckGo scrape if SearXNG is down.
        snips: list[str] = []
        try:
            from otools import searxng_tool
            snips = await searxng_tool.searx_web(subject, max_snippets)
        except Exception as e:
            log.warning(f"[research] searxng web failed: {e}")
        if not snips:
            snips = await _ddg_snippets(client, subject, max_snippets)
            if len(snips) < max_snippets:
                snips += await _ddg_snippets(client, f"{subject} latest news", max_snippets - len(snips))
        for s in snips[:max_snippets]:
            parts.append(f"[Web] {s}")
    return "\n".join(parts)
