"""News headlines tool — fetches RSS from major outlets. No API key needed."""

import logging
import re
from xml.etree import ElementTree as ET

import httpx

from tool_registry import tool
from jtools.cache_util import ttl_cache

log = logging.getLogger("jarvis.tools.news")

# Pre-vetted RSS feeds by topic. All work without a key.
# NOTE: Reuters public RSS (feeds.reuters.com) was shut down and no longer
# resolves — replaced with Guardian + Al Jazeera so each topic has multiple
# LIVE sources. Callers should pull a few items PER feed (not all from the
# first) so headlines actually vary instead of looking like a static template.
FEEDS = {
    "top": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.theguardian.com/world/rss",
        "https://feeds.npr.org/1001/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "tech": [
        "https://feeds.arstechnica.com/arstechnica/technology-lab",
        "https://www.theverge.com/rss/index.xml",
        "https://techcrunch.com/feed/",
    ],
    "science": [
        "https://www.sciencedaily.com/rss/top/science.xml",
        "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "https://www.theguardian.com/science/rss",
    ],
    "business": [
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.theguardian.com/uk/business/rss",
        "https://feeds.npr.org/1006/rss.xml",
    ],
    "sports": [
        "https://feeds.bbci.co.uk/sport/rss.xml",
        "https://www.theguardian.com/uk/sport/rss",
    ],
    "world": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.theguardian.com/world/rss",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
}

_HEADERS = {"User-Agent": "JARVIS-OS/0.2 news-fetcher"}


def _parse_rss(xml_text: str, max_items: int) -> list[str]:
    """Extract headlines from an RSS feed."""
    titles: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return titles
    # RSS 2.0: channel/item/title.  Atom: entry/title.
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag in ("item", "entry"):
            for child in item:
                ctag = child.tag.split("}")[-1]
                if ctag == "title":
                    text = (child.text or "").strip()
                    if text:
                        titles.append(text)
                    break
        if len(titles) >= max_items:
            break
    return titles[:max_items]


@tool(
    name="get_news",
    description=(
        "Fetch the top news headlines from major outlets (BBC, Reuters, NPR, etc.). "
        "Use this when the user asks 'what's in the news', 'any headlines', 'tell me "
        "about the news', or asks about current events on a topic. Returns 3-5 "
        "headlines as plain text."
    ),
    parameters={
        "topic": {
            "type": "string",
            "description": "One of: top, tech, science, business, sports, world. Default 'top'.",
            "enum": ["top", "tech", "science", "business", "sports", "world"],
        },
        "count": {
            "type": "integer",
            "description": "How many headlines to return (1-5, default 3).",
        },
    },
    required=[],
)
@ttl_cache(600)  # headlines don't turn over meaningfully minute to minute
async def get_news(topic: str = "top", count: int = 3) -> str:
    topic = (topic or "top").lower()
    if topic not in FEEDS:
        topic = "top"
    count = max(1, min(int(count or 3), 5))
    feeds = FEEDS[topic]
    headlines: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS, follow_redirects=True) as client:
            for feed_url in feeds:
                if len(headlines) >= count:
                    break
                try:
                    r = await client.get(feed_url)
                    if r.status_code != 200:
                        continue
                    for t in _parse_rss(r.text, count):
                        # Dedupe by simplified title
                        sig = re.sub(r"[^a-z0-9]", "", t.lower())[:60]
                        if sig and sig not in {re.sub(r"[^a-z0-9]", "", h.lower())[:60] for h in headlines}:
                            headlines.append(t)
                        if len(headlines) >= count:
                            break
                except Exception:
                    continue
    except Exception as e:
        log.error(f"[news] {e}")
        return "I couldn't reach the news services."

    if not headlines:
        return f"I couldn't pull any {topic} headlines just now."

    pretty = ". ".join(h.rstrip(".") for h in headlines[:count])
    label = "Top stories" if topic == "top" else f"{topic.capitalize()} headlines"
    return f"{label}: {pretty}."
