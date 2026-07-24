"""Web scraper tool — fetch a URL and return its main text content."""

import logging
import re

import httpx

from tool_registry import tool

log = logging.getLogger("orb.tools.scraper")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JARVIS-OS/0.2",
    "Accept": "text/html,application/xhtml+xml",
}


def _strip_html(html: str) -> str:
    """Crude but reliable HTML -> plain text. No BeautifulSoup dep needed for a one-shot scrape."""
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<noscript[\s\S]*?</noscript>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<!--[\s\S]*?-->", " ", html)
    # block tags -> newline so paragraphs survive
    html = re.sub(r"</(p|div|h[1-6]|li|tr|br|article|section)>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    # decode common entities
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


@tool(
    name="scrape_url",
    description=(
        "Fetch a web page and return its main text content. Use this when the "
        "user explicitly references a URL or asks you to 'check this site', "
        "'read this article', or 'see what's on'. Returns up to ~2000 chars "
        "of cleaned text. Do NOT use for general web search — use lookup_fact "
        "for that."
    ),
    parameters={
        "url": {
            "type": "string",
            "description": "Full URL including https://. If the user says 'github.com/foo', prepend https://.",
        },
        "max_chars": {
            "type": "integer",
            "description": "Maximum characters to return (default 2000, cap 8000).",
        },
    },
    required=["url"],
)
async def scrape_url(url: str, max_chars: int = 2000) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    max_chars = max(100, min(int(max_chars or 2000), 8000))
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=_HEADERS, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
                return f"That URL returned {ctype} — not a readable page."
            text = _strip_html(r.text)
            if not text:
                return f"I fetched {url} but found no readable text."
            return text[:max_chars] + ("..." if len(text) > max_chars else "")
    except httpx.HTTPStatusError as e:
        return f"The site returned status {e.response.status_code}."
    except Exception as e:
        log.error(f"[scrape] {e}")
        return f"I couldn't reach {url}."
