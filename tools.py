"""
Real tools JARVIS can use itself (no Claude Code needed):
- weather  (open-meteo, free, no API key)
- web search (DuckDuckGo, free, no API key)

These let Qwen actually answer "what's the weather" / "look up X" instead of
hallucinating. Only genuinely complex multi-step work escalates to Claude Code.
"""

import logging
import re
import urllib.parse

import os

import httpx

log = logging.getLogger("jarvis")

# Fallback when no location is spoken. Env-driven since 2026-07-03 (OSS prep —
# the operator's city is personal config, not source). History: this sat on a
# hardcoded city ~90mi off until the 07-02 test battery caught a no-location
# weather ask answering for the wrong place — set JARVIS_DEFAULT_LOCATION.
DEFAULT_LOCATION = os.getenv("JARVIS_DEFAULT_LOCATION", "New York")

# US states -> a sensible major city (so "weather in North Carolina" doesn't
# resolve to some tiny matching town). Lowercased keys.
US_STATE_TO_CITY = {
    "alabama": "Birmingham", "alaska": "Anchorage", "arizona": "Phoenix",
    "arkansas": "Little Rock", "california": "Los Angeles", "colorado": "Denver",
    "connecticut": "Hartford", "delaware": "Wilmington", "florida": "Miami",
    "georgia": "Atlanta", "hawaii": "Honolulu", "idaho": "Boise", "illinois": "Chicago",
    "indiana": "Indianapolis", "iowa": "Des Moines", "kansas": "Wichita",
    "kentucky": "Louisville", "louisiana": "New Orleans", "maine": "Portland",
    "maryland": "Baltimore", "massachusetts": "Boston", "michigan": "Detroit",
    "minnesota": "Minneapolis", "mississippi": "Jackson", "missouri": "Kansas City",
    "montana": "Billings", "nebraska": "Omaha", "nevada": "Las Vegas",
    "new hampshire": "Manchester", "new jersey": "Newark", "new mexico": "Albuquerque",
    "new york": "New York", "north carolina": "Charlotte", "north dakota": "Fargo",
    "ohio": "Columbus", "oklahoma": "Oklahoma City", "oregon": "Portland",
    "pennsylvania": "Philadelphia", "rhode island": "Providence",
    "south carolina": "Columbia", "south dakota": "Sioux Falls", "tennessee": "Nashville",
    "texas": "Houston", "utah": "Salt Lake City", "vermont": "Burlington",
    "virginia": "Virginia Beach", "washington": "Seattle", "west virginia": "Charleston",
    "wisconsin": "Milwaukee", "wyoming": "Cheyenne",
}

# --- intent detection ---------------------------------------------------------

WEATHER_WORDS = ["weather", "forecast", "temperature", "how hot", "how cold",
                 "is it going to rain", "will it rain", "how warm", "degrees outside"]
SEARCH_PREFIXES = ["search for", "look up", "google", "what is", "who is",
                   "what are", "tell me about", "look into"]


def is_weather_query(text: str) -> bool:
    return any(w in text.lower() for w in WEATHER_WORDS)


def extract_location(text: str) -> str | None:
    """Pull a location out of 'weather in North Carolina' style phrasing."""
    m = re.search(r"\bin ([A-Za-z][A-Za-z .,'-]+?)(?:\s+(?:today|tomorrow|tonight|now|this week))?[?.!]?$",
                  text.strip(), re.IGNORECASE)
    if m:
        loc = m.group(1).strip(" .,?!")
        if len(loc) > 1:
            return loc
    return None


def wants_tomorrow(text: str) -> bool:
    return "tomorrow" in text.lower()


# --- weather ------------------------------------------------------------------

_WEATHER_CODES = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


async def _geocode(client: httpx.AsyncClient, query: str) -> dict | None:
    """Try several variants of a place query and pick the best-populated hit."""
    # Try the original, and stripped-comma variants, and just the first token
    variants = [query, query.replace(",", "").strip()]
    # If "City State" given without comma, also try just the first part
    parts = query.replace(",", "").split()
    if len(parts) >= 2:
        variants.append(parts[0])
        variants.append(" ".join(parts[:2]))
    seen = set()
    candidates: list[dict] = []
    for v in variants:
        v = v.strip()
        if not v or v.lower() in seen:
            continue
        seen.add(v.lower())
        try:
            r = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": v, "count": 10, "language": "en", "format": "json"},
            )
            r.raise_for_status()
            for res in (r.json().get("results") or []):
                candidates.append(res)
        except Exception:
            continue
    if not candidates:
        return None
    # If the query mentions a US state, prefer hits in that state
    q_lower = query.lower()
    state_matches = [c for c in candidates if c.get("admin1") and c["admin1"].lower() in q_lower]
    pool = state_matches or candidates
    # Among the pool, prefer the highest-population result (handles "North Carolina" -> not Calvary)
    pool.sort(key=lambda c: c.get("population") or 0, reverse=True)
    return pool[0]


async def get_weather(location: str | None, tomorrow: bool = False) -> str:
    """Return a one-sentence spoken weather report. Uses open-meteo (no key)."""
    loc = location or DEFAULT_LOCATION
    # If the user said only a US state, swap in that state's major city
    if loc.lower().strip(" ,.") in US_STATE_TO_CITY:
        loc = US_STATE_TO_CITY[loc.lower().strip(" ,.")] + ", " + loc.strip()
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            place = await _geocode(client, loc)
            if not place:
                return f"I couldn't find a place called {loc}."
            lat, lon = place["latitude"], place["longitude"]
            admin = place.get("admin1")
            name = place.get("name", loc)
            if admin and admin.lower() not in name.lower():
                name = f"{name}, {admin}"

            fc = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weathercode",
                    "temperature_unit": "fahrenheit", "timezone": "auto",
                },
            )
            fc.raise_for_status()
            daily = fc.json()["daily"]
            idx = 1 if tomorrow and len(daily["time"]) > 1 else 0
            hi = round(daily["temperature_2m_max"][idx])
            lo = round(daily["temperature_2m_min"][idx])
            pop = daily["precipitation_probability_max"][idx]
            code = daily["weathercode"][idx]
            cond = _WEATHER_CODES.get(code, "mixed conditions")
            when = "Tomorrow" if idx == 1 else "Today"
            rain = f", {pop}% chance of precipitation" if pop and pop >= 20 else ""
            return f"{when} in {name}: {cond}, high of {hi} and low of {lo} degrees{rain}."
    except Exception as e:
        log.error(f"[weather] {e}")
        return "I couldn't reach the weather service just now."


# --- web search ---------------------------------------------------------------

_WIKI_HEADERS = {
    # Wikipedia's API policy requires a specific UA format with contact info.
    "User-Agent": "Orb-Backend/0.1 (https://github.com/getorb/Orb-Backend) python-httpx",
    "Accept": "application/json",
    "Api-User-Agent": "Orb-Backend/0.1 (https://github.com/getorb/Orb-Backend)",
}


async def _wiki_summary(client: httpx.AsyncClient, query: str) -> str:
    """Find a Wikipedia title via opensearch or content-search, then summarize it."""
    title: str | None = None

    # 1) opensearch — matches titles, fast, lenient. Works for "Tony Stark".
    o = await client.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "opensearch", "search": query, "limit": 3,
                "namespace": 0, "format": "json"},
    )
    if o.status_code == 200:
        data = o.json()
        if isinstance(data, list) and len(data) > 1 and data[1]:
            title = data[1][0]

    # 2) content search fallback — for "population of X" / "history of Y" style queries
    if not title:
        s2 = await client.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query,
                    "format": "json", "srlimit": 1},
        )
        if s2.status_code == 200:
            hits = s2.json().get("query", {}).get("search", [])
            if hits:
                title = hits[0]["title"]

    if not title:
        return ""

    # Relevance gate: reject articles whose title shares no meaningful words
    # with the query (e.g. "hollow agent os" → "Zenless Zone Zero" is wrong).
    _stop = {"the", "a", "an", "of", "in", "is", "what", "who", "how", "for", "and", "or"}
    q_words = {w.lower() for w in re.split(r'\W+', query) if len(w) > 2} - _stop
    t_words = {w.lower() for w in re.split(r'\W+', title) if len(w) > 2} - _stop
    if q_words and not (q_words & t_words):
        return ""  # Wrong article; fall through to DuckDuckGo

    s = await client.get(
        f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
    )
    if s.status_code != 200:
        return ""
    extract = (s.json() or {}).get("extract") or ""
    if not extract:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", extract)
    return " ".join(sentences[:2]).strip()


async def web_search(query: str) -> str:
    """Factual lookup. Tries Wikipedia (great for entities/topics), then DDG."""
    try:
        async with httpx.AsyncClient(timeout=12.0, headers=_WIKI_HEADERS,
                                     follow_redirects=True) as client:
            wiki = await _wiki_summary(client, query)
            if wiki:
                return wiki
            # Fallback: DuckDuckGo Instant Answer
            r = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            )
            if r.status_code == 200:
                data = r.json()
                for key in ("AbstractText", "Answer", "Definition"):
                    val = data.get(key)
                    if val:
                        return val
        return ""
    except Exception as e:
        log.error(f"[search] {e}")
        return ""
