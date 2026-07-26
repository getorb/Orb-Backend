"""Stock / market data — keyless, via Yahoo Finance's public chart + search
endpoints. Returns a price series the HUD card draws as a line graph.

(Stooq started requiring an API key; Yahoo's v8 chart endpoint is keyless.)
"""

import logging
import re

import httpx

from tool_registry import tool
from otools.cache_util import ttl_cache

log = logging.getLogger("orb.tools.finance")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Spoken names -> Yahoo symbols for indices and the most-asked tickers.
_INDEX = {
    "s&p 500": "^GSPC", "s&p500": "^GSPC", "s and p 500": "^GSPC", "sp500": "^GSPC",
    "s&p": "^GSPC", "spx": "^GSPC", "nasdaq": "^IXIC", "dow": "^DJI",
    "dow jones": "^DJI", "russell 2000": "^RUT", "russell": "^RUT",
    "vix": "^VIX", "ftse": "^FTSE", "ftse 100": "^FTSE", "nikkei": "^N225",
    "bitcoin": "BTC-USD", "ethereum": "ETH-USD", "gold": "GC=F", "oil": "CL=F",
}
_COMPANY = {
    "tesla": "TSLA", "apple": "AAPL", "google": "GOOGL", "alphabet": "GOOGL",
    "microsoft": "MSFT", "amazon": "AMZN", "nvidia": "NVDA", "meta": "META",
    "facebook": "META", "netflix": "NFLX", "disney": "DIS", "boeing": "BA",
    "coca cola": "KO", "coca-cola": "KO", "mcdonald's": "MCD", "mcdonalds": "MCD",
    "walmart": "WMT", "intel": "INTC", "amd": "AMD", "palantir": "PLTR",
    "gamestop": "GME", "ford": "F", "starbucks": "SBUX",
}


async def _yahoo_search(client: httpx.AsyncClient, q: str) -> tuple[str, str] | None:
    """Resolve any company/asset name -> (symbol, display name) via Yahoo search."""
    try:
        r = await client.get("https://query1.finance.yahoo.com/v1/finance/search",
                             params={"q": q, "quotesCount": 1, "newsCount": 0})
        if r.status_code == 200:
            quotes = r.json().get("quotes", [])
            if quotes:
                sym = quotes[0].get("symbol")
                name = (quotes[0].get("shortname") or quotes[0].get("longname") or sym)
                if sym:
                    return sym, name
    except Exception as e:
        log.warning(f"[finance] yahoo search failed: {e}")
    return None


def _strip_to_subject(query: str) -> str:
    """Remove the 'stock / price / show me a graph of' framing to get the name."""
    q = query.lower()
    q = re.sub(r"\b(show me|pull up|what'?s|what is|how is|how'?s|the|a|graph|chart|"
               r"of|for|stock|stocks|share|shares|price|prices|ticker|doing|today|"
               r"right now|lifetime|all[- ]?time|history|historical)\b", " ", q)
    return re.sub(r"\s{2,}", " ", q).strip(" ?.,")


@ttl_cache(120)  # short — price data, but still saves repeated Yahoo hits
                 # within a burst (e.g. a chat check landing near an hourly watch poll)
async def get_stock(query: str) -> dict | None:
    """Resolve `query` to a symbol and return a price series + current quote.
    Returns None if it can't be resolved/fetched."""
    raw = (query or "").strip()
    if not raw:
        return None
    rng = "max" if re.search(r"\b(lifetime|all[- ]?time|max|history|since inception)\b", raw, re.I) else "1y"
    low = raw.lower()
    symbol = name = None
    # 1) index keywords anywhere in the phrase
    for k, sym in _INDEX.items():
        if k in low:
            symbol, name = sym, k.upper(); break
    subject = _strip_to_subject(raw)
    if not symbol:
        # 2) company map
        if subject in _COMPANY:
            symbol, name = _COMPANY[subject], subject.title()
        # 3) bare ticker ("AAPL", "TSLA stock")
        elif re.fullmatch(r"[A-Za-z]{1,5}", subject or ""):
            symbol = name = subject.upper()

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as client:
        if not symbol:
            # 4) free-text search via Yahoo
            hit = await _yahoo_search(client, subject or raw)
            if hit:
                symbol, name = hit
        if not symbol:
            return None
        try:
            r = await client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"range": rng, "interval": "1d" if rng != "max" else "1wk"})
            if r.status_code != 200:
                return None
            res = r.json().get("chart", {}).get("result")
            if not res:
                return None
            meta = res[0]["meta"]
            closes = res[0]["indicators"]["quote"][0].get("close") or []
            pts = [round(x, 2) for x in closes if x is not None]
            if len(pts) < 2:
                return None
        except Exception as e:
            log.warning(f"[finance] chart fetch failed for {symbol}: {e}")
            return None

    price = meta.get("regularMarketPrice", pts[-1])
    cur = meta.get("currency", "USD")
    first = pts[0]
    change = ((price - first) / first * 100) if first else 0.0

    def _delta(base):
        if base:
            d = price - base
            return [round(d, 2), round(d / base * 100, 1)]
        return None
    # day / month / year dollar+percent moves (meaningful on the daily 1y series)
    day = month = year = None
    if rng == "1y":
        if len(pts) >= 2:
            day = _delta(pts[-2])
        month = _delta(pts[-23]) if len(pts) >= 23 else None
        year = _delta(first)

    return {
        "symbol": symbol, "name": name or symbol, "price": price, "currency": cur,
        "change_pct": round(change, 1), "points": pts, "range": rng,
        "up": change >= 0, "day": day, "month": month, "year": year,
    }


@tool(
    name="get_stock",
    description=("Get a stock or market index price and history to chart. Use for "
                 "'X stock', 'S&P 500', 'price of X', a ticker, crypto, etc."),
    parameters={"query": {"type": "string", "description": "Company name, ticker, or index."}},
    required=["query"],
)
async def get_stock_tool(query: str) -> str:
    d = await get_stock(query)
    if not d:
        return f"I couldn't find market data for {query}."
    arrow = "up" if d["up"] else "down"
    return (f"{d['name']} is at {d['price']:.2f} {d['currency']}, "
            f"{arrow} {abs(d['change_pct'])}% over the period.")
