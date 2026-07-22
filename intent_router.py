"""
Deterministic intent router.

Maps a user transcript to (tool_name, args) WITHOUT consulting the LLM.
Fast, reliable, and never says "I don't have a tool for that" when a
tool clearly applies.

Order of checks is most-specific to most-general. Each matcher returns
a dict of args, or None if it doesn't apply. The first non-None match
wins. Falling through to None means the request goes to the LLM
fallback path (free-form conversation or LLM tool-calling).

The naturalization layer in server_win wraps the tool's raw output in
JARVIS-voice prose; this router itself only decides WHICH tool runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Intent:
    tool: str
    args: dict


# ---------------------------------------------------------------------------
# Helper regexes
# ---------------------------------------------------------------------------

# Math-y phrases that should hit `calculate`. Triggered by either an
# explicit "calculate"/"compute" verb, or an obvious arithmetic expression.
# NOTE: word boundaries on every verb. Without \b at the END of "compute",
# "computer" matches it and "how long has my computer been running" was
# misrouted to calculate.
_MATH_VERBS = re.compile(
    r"\b(calculate|calculates|compute\b|computes\b|"
    r"what(?:'s| is) (?:the\s+)?(?:square root|sqrt|sine|cosine|tangent|log|exp|cube root)|"
    r"(?:what(?:'s| is)|how much is)\s+\d)",
    re.IGNORECASE)
_MATH_EXPR = re.compile(
    r"^\s*[\d.()+\-*/^%×÷,\s]+\s*$")  # bare expressions like "(3+4)*5"
_PERCENT = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+([\d.,]+)", re.IGNORECASE)
_TIMES = re.compile(
    r"\b([\d.]+)\s+(?:times|x|\*|multiplied by)\s+([\d.]+)", re.IGNORECASE)
_PLUS = re.compile(
    r"\b([\d.]+)\s+(?:plus|\+|added to)\s+([\d.]+)", re.IGNORECASE)
_MINUS = re.compile(
    r"\b([\d.]+)\s+(?:minus|\-|subtracted from)\s+([\d.]+)", re.IGNORECASE)
_DIVIDED = re.compile(
    r"\b([\d.]+)\s+(?:divided by|over|/)\s+([\d.]+)", re.IGNORECASE)

# Weather
_WEATHER_WORDS = re.compile(
    r"\b(weather|forecast|temperature|how hot|how cold|how warm|"
    r"is it going to rain|will it rain|degrees outside|raining|snowing)\b",
    re.IGNORECASE)
_LOCATION_IN = re.compile(
    r"\bin\s+([A-Za-z][A-Za-z .,'-]+?)(?:\s+(?:today|tomorrow|tonight|now|this week))?[?.!]?$",
    re.IGNORECASE)
_TOMORROW = re.compile(r"\btomorrow\b", re.IGNORECASE)

# News
_NEWS_WORDS = re.compile(
    r"\b(news|headlines?|what(?:'s| is) (?:going on|happening)|"
    r"current events|latest stories)\b", re.IGNORECASE)
_NEWS_TOPICS = {
    "tech": ("tech", "technology", "ai", "software", "computer"),
    "science": ("science", "scientific", "research"),
    "business": ("business", "finance", "stock market", "economy"),
    "sports": ("sports", "sport", "game", "football", "basketball"),
    "world": ("world", "international", "global", "foreign"),
}

# System info
_SYS_WORDS = re.compile(
    r"\b(my (?:cpu|ram|memory|disk|system|computer|pc)|"
    r"how (?:much )?(?:cpu|ram|memory|disk|storage) (?:is|am i)|"
    r"system (?:status|info|stats)|uptime|how long (?:has|have) (?:my|the) (?:computer|pc|system))",
    re.IGNORECASE)
_SYS_METRICS = {
    "cpu":     re.compile(r"\bcpu\b", re.IGNORECASE),
    "memory":  re.compile(r"\b(ram|memory)\b", re.IGNORECASE),
    "disk":    re.compile(r"\b(disk|storage|drive)\b", re.IGNORECASE),
    "uptime":  re.compile(r"\b(uptime|how long.*(?:running|up|on))\b", re.IGNORECASE),
}

# Notes
_NOTE_ADD = re.compile(
    r"^(?:hey\s+)?(?:remember(?: that)?|note (?:that|to self)|"
    r"jot down|write down|save (?:this|a note)|remind me (?:that|to)|"
    r"take a note(?:\s+that)?)\s+(.+)$",
    re.IGNORECASE)
_NOTE_LIST = re.compile(
    r"\b(what (?:are )?my notes|list (?:my )?notes|read (?:back )?my notes|"
    r"show (?:me )?my notes|what'?s on my list)\b", re.IGNORECASE)
_NOTE_SEARCH = re.compile(
    r"(?:find|search) (?:my notes? )?(?:for |about )(.+)", re.IGNORECASE)

# Files (sandboxed)
_FILE_READ = re.compile(
    r"\b(?:read|open|show)(?:\s+me)?\s+(?:my\s+|the\s+)?file\s+"
    r"(?:called\s+|named\s+)?[\"']?([\w.\-/ ]+?)[\"']?\s*$",
    re.IGNORECASE)
_FILE_LIST = re.compile(
    r"\b(?:list|show) (?:my )?(?:files|jarvis files|files in (?:my )?jarvis folder)\b",
    re.IGNORECASE)

# URL detection (scrape)
_URL_RE = re.compile(
    r"\b((?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[\w\-./?%&=]*)?)",
    re.IGNORECASE)
_SCRAPE_VERB = re.compile(
    r"\b(read|fetch|scrape|check|summarize|look at|what(?:'s| is) on|"
    r"what does .* say)\b", re.IGNORECASE)

# Factual lookup — broad catchall. Comes LAST so specific tools win first.
# Includes the failure case ninjahawk hit: "how many calories in a gallon of milk".
_LOOKUP_OPENERS = (
    "who is", "who was", "who are", "who were", "who's",
    "what is", "what was", "what are", "what's", "what were",
    "when is", "when was", "when did", "when does", "when will", "when do",
    "where is", "where was", "where are", "where did",
    "how tall", "how big", "how old", "how many", "how much",
    "how far", "how long", "how does", "how do", "how did", "how is",
    "why is", "why was", "why are", "why did", "why do", "why does",
    "how to", "how can", "how would", "how should",
    "which", "compare", "explain", "are there", "is there",
    "best ", "top ", "list ", "what kind", "what type", "what makes", "what are the",
    "tell me about", "look up", "define", "look into",
)
# Avoid hitting Wikipedia for personal-context questions about the user
# Personal-context pronouns -> skip lookup/research (e.g. "what's my name").
# NOTE: 'us'/'we' deliberately excluded — 'us' collides with U.S. ("the US
# economy", "US history") and wrongly suppressed those.
_PERSONAL = re.compile(
    r"\b(my|your|you|me|our)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Individual matchers
# ---------------------------------------------------------------------------

def _strip_punct(text: str) -> str:
    return text.strip().strip(",.!?").strip()


def match_scrape(text: str) -> dict | None:
    """Trigger scrape_url when the user references a URL with intent to read it."""
    m = _URL_RE.search(text)
    if not m:
        return None
    url = m.group(1)
    # Reject things like "1.5" / "v0.3.0" / version-looking numerics
    if re.fullmatch(r"[\d.]+", url):
        return None
    # Need a verb that implies fetching, OR an explicit https://
    if "://" in url or _SCRAPE_VERB.search(text):
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        return {"url": url}
    return None


def match_calculate(text: str) -> dict | None:
    """Recognize calculation requests across natural-language forms."""
    t = text.strip().rstrip("?.!")
    # Explicit calculate/compute verb
    if _MATH_VERBS.search(t):
        # Try to extract just the expression part
        expr = re.sub(r"^.*?(?:calculate|compute|what(?:'s| is)|how much is)\s+", "", t,
                      flags=re.IGNORECASE).strip()
        expr = re.sub(r"^(?:the\s+)", "", expr, flags=re.IGNORECASE).strip()
        # Substitute "square root of N" -> "sqrt(N)"
        expr = re.sub(r"\bsquare root of\s+", "sqrt ", expr, flags=re.IGNORECASE)
        expr = re.sub(r"\bsqrt\s+(\S+)", r"sqrt(\1)", expr)
        if expr:
            return {"expression": expr}
    # Bare expression
    if _MATH_EXPR.match(t) and re.search(r"[\d]", t) and re.search(r"[+\-*/^]", t):
        return {"expression": t}
    # "X% of Y"
    m = _PERCENT.search(t)
    if m:
        pct, whole = m.group(1), m.group(2).replace(",", "")
        return {"expression": f"({pct} / 100) * {whole}"}
    # "X times Y", "X plus Y", "X divided by Y", "X minus Y"
    for pat in (_TIMES, _PLUS, _MINUS, _DIVIDED):
        m = pat.search(t)
        if m:
            a, b = m.group(1), m.group(2)
            op = {_TIMES: "*", _PLUS: "+", _MINUS: "-", _DIVIDED: "/"}[pat]
            return {"expression": f"{a} {op} {b}"}
    return None


def match_weather(text: str) -> dict | None:
    if not _WEATHER_WORDS.search(text):
        return None
    loc_m = _LOCATION_IN.search(text)
    args: dict = {}
    if loc_m:
        loc = _strip_punct(loc_m.group(1))
        if len(loc) > 1 and loc.lower() not in (
            "the morning", "the afternoon", "the evening", "an hour", "a bit"):
            args["location"] = loc
    if "location" not in args:
        # let the weather tool fall back to its DEFAULT_LOCATION
        args["location"] = ""
    args["tomorrow"] = bool(_TOMORROW.search(text))
    return args


def match_news(text: str) -> dict | None:
    if not _NEWS_WORDS.search(text):
        return None
    lower = text.lower()
    topic = "top"
    for cat, keywords in _NEWS_TOPICS.items():
        if any(k in lower for k in keywords):
            topic = cat
            break
    # Try to extract a count ("give me 5 headlines", "5 tech headlines",
    # "5 top stories"). Allow up to a couple of words between the digit and
    # the noun.
    cm = re.search(r"\b(\d+)\s+(?:\w+\s+){0,2}(?:headlines?|stories|articles)",
                   text, re.IGNORECASE)
    count = int(cm.group(1)) if cm else 3
    count = max(1, min(count, 5))
    return {"topic": topic, "count": count}


def match_system(text: str) -> dict | None:
    if not _SYS_WORDS.search(text):
        return None
    for metric, pat in _SYS_METRICS.items():
        if pat.search(text):
            return {"metric": metric}
    return {"metric": "all"}


def match_note_add(text: str) -> dict | None:
    m = _NOTE_ADD.match(text.strip())
    if not m:
        return None
    body = _strip_punct(m.group(1))
    if not body:
        return None
    return {"text": body}


def match_note_list(text: str) -> dict | None:
    return {} if _NOTE_LIST.search(text) else None


def match_note_search(text: str) -> dict | None:
    m = _NOTE_SEARCH.search(text)
    if not m:
        return None
    q = _strip_punct(m.group(1))
    return {"query": q} if q else None


def match_file_read(text: str) -> dict | None:
    m = _FILE_READ.search(text)
    if not m:
        return None
    return {"name": _strip_punct(m.group(1))}


def match_file_list(text: str) -> dict | None:
    return {} if _FILE_LIST.search(text) else None


def match_lookup(text: str) -> dict | None:
    """Catch-all factual question router. MUST come last."""
    t = _strip_punct(text).lower()
    if not t:
        return None
    # Must start with a factual-question opener
    if not any(t.startswith(opener) for opener in _LOOKUP_OPENERS):
        return None
    # Skip personal-context questions ("what's my name", "how are you"),
    # but FIRST strip leading idioms where "me" doesn't refer to the user:
    #   "tell me about X", "show me X", "give me X", "let me X"
    cleaned = re.sub(r"^(?:tell|show|give|let)\s+me\b", "", t).strip()
    if _PERSONAL.search(cleaned[:40]):
        return None
    return {"query": _strip_punct(text)}


_IMAGE_OF_RE = re.compile(
    r"\b(?:picture|pictures|photo|photos|image|images|pic|pics)\s+of\s+(.+)", re.I)
_LOOKS_LIKE_RE = re.compile(r"\bwhat\s+do(?:es)?\s+(.+?)\s+look\s+like\b", re.I)


def match_search_images(text: str) -> dict | None:
    """Route 'show me a picture/photo/image of X' and 'what does X look like'
    to live image search -> a photo grid in the HUD. More specific than the
    lookup catch-all, so it must come before it."""
    t = _strip_punct(text).strip()
    if not t:
        return None
    m = _LOOKS_LIKE_RE.search(t)
    if not m:
        m = _IMAGE_OF_RE.search(t)
    if not m:
        return None
    q = m.group(1).strip()
    # trim trailing politeness so the search query stays clean
    q = re.sub(r"\b(please|thanks|thank you|for me)\b\.?\s*$", "", q, flags=re.I).strip()
    if not q:
        return None
    return {"query": q, "count": 6}


_SHOW_MORE_RE = re.compile(
    r"^(?:show me|let me see|let'?s see|can i see|i (?:want|wanna|would like|'?d like) to see|see)"
    r"(?:\s+(?:it|that|this|them|those|some|a few|more|the|him|her|its|please|"
    r"pictures?|images?|photos?|pics?|what (?:it|they|he|she) looks? like))*\s*[?.!]*$",
    re.I)


def match_show_more(text: str) -> dict | None:
    """Bare visual follow-up with NO new subject — 'show me', 'show me more',
    'let me see it'. Resolves to the most-recently-discussed subject in the
    handler (state['last_subject']). 'show me a picture of X' still routes to
    search_images (it has a subject)."""
    t = _strip_punct(text).strip()
    return {} if _SHOW_MORE_RE.match(t) else None


_STOCK_RE = re.compile(
    r"\b(stocks?|share price|stock price|shares|ticker|nasdaq|dow(?:\s+jones)?|"
    r"s&p\s*500|s and p\s*500|sp\s*500|russell|nikkei|ftse|bitcoin|ethereum|"
    r"crypto|market cap|trading at|gold|silver|oil|crude|platinum|copper|"
    r"price of|current price|how much is|what'?s .* worth)\b", re.I)


# Screenshot / "show me my screen" — the desktop bridge (ORB_ROADMAP Pillar A).
# Deliberately narrow: the word "screenshot", OR a show/send/mirror verb tied to
# "my/the/your screen|desktop|display", OR a "what's on my screen / what are you
# looking at" question. 'pc'/'computer' are NOT objects here — they collide with
# system-info ("how long has my computer been running"); 'screen'/'desktop' don't.
_SCREENSHOT_WORD = re.compile(r"\b(screenshot|screen shot)\b", re.I)
_SCREEN_SHOW = re.compile(
    r"\b(?:take|grab|capture|send|show|see|mirror|share|view|pull up|let me see)\s+"
    r"(?:me\s+)?(?:my|the|your)\s+(?:screen|desktop|display)\b", re.I)
_SCREEN_WHATS = re.compile(
    r"\bwhat(?:'?s|s| is| are you| do you| can you)?\b[\w\s']*?\b"
    r"(?:on (?:my|the|your) (?:screen|desktop|display)|"
    r"(?:are you |do you )?looking at)\b", re.I)


def match_screenshot(text: str) -> dict | None:
    """'take a screenshot' / 'show me my screen' / 'what's on my screen' /
    'what are you looking at' -> capture the desktop and send it (a card panel on
    mobile, a saved file on desktop). Read-only; never injects input."""
    t = _strip_punct(text)
    if not t:
        return None
    if _SCREENSHOT_WORD.search(t) or _SCREEN_SHOW.search(t) or _SCREEN_WHATS.search(t):
        return {}
    return None


_GAMES = {"snake": "snake.html", "pong": "pong.html", "2048": "2048.html",
          "twenty forty eight": "2048.html", "twenty forty-eight": "2048.html"}
_PLAY_RE = re.compile(r"\b(play|let'?s play|start|launch|open|fire up)\b", re.I)


def match_open_chat(text: str) -> dict | None:
    """'open chat' / 'let me type' / 'open a chat window' -> the typed chat HUD."""
    t = _strip_punct(text).lower()
    if re.search(r"\bchat\b", t) and re.search(r"\b(open|bring up|launch|start|show|pull up|give me)\b", t):
        return {}
    if re.search(r"\b(let me|i (?:want|wanna|'?d like) to)\s+type\b", t):
        return {}
    return None


def match_game(text: str) -> dict | None:
    """'play snake' / 'let's play 2048' -> launch a self-hosted HTML5 game in a
    HUD card. Only matches when a known game is named alongside a play verb."""
    t = _strip_punct(text).lower()
    if not _PLAY_RE.search(t):
        return None
    for name, f in _GAMES.items():
        if name in t:
            return {"game": f}
    return None


_GRAPH_RE = re.compile(r"\b(graph|chart|plot|over time|per year|by year|over the years|trend)\b", re.I)


def match_graph(text: str) -> dict | None:
    """Custom data graph ('graph of YouTube revenue', 'X over time') -> a
    Haiku-assembled chart. Runs AFTER get_stock so real tickers chart from live
    data; deliberately excludes 'history of' (that's a research request)."""
    t = _strip_punct(text)
    if not _GRAPH_RE.search(t):
        return None
    q = re.sub(r"\b(show me|pull up|can you|could you|please|a|an|the|graph|chart|"
               r"plot|of|for|me|over time|per year|by year|over the years|trend)\b",
               " ", t, flags=re.I)
    q = re.sub(r"\s{2,}", " ", q).strip(" ?.,")
    return {"query": q or t}


_STOCK_INFO_RE = re.compile(
    r"\b(strateg|advice|tip|how to|how do|explain|learn|invest|should i|best way|"
    r"guide|beginner|understand|risk|what are the best|which|tell me about)", re.I)


def match_stock(text: str) -> dict | None:
    """Stock / index / crypto PRICE queries -> a price chart. Skips EDUCATIONAL
    'stock' questions ('best stock strategies', 'how to invest') — those are
    informational and should go to research, not a ticker lookup."""
    t = _strip_punct(text)
    if not _STOCK_RE.search(t):
        return None
    if _STOCK_INFO_RE.search(t):
        return None
    return {"query": t}


_DEEP_RE = re.compile(
    r"^(?:do (?:some )?research (?:on|about|into)|research|deep[- ]?dive (?:on|into)|"
    r"go (?:in[- ]?depth|deep) on|look into|investigate)\b\s+(.+)", re.I)


def match_deep_research(text: str) -> dict | None:
    """'research X' / 'deep dive on X' / 'investigate X' -> multi-source AUTO
    research: parallel fetches that pop several cards, then a grounded synthesis
    card. More specific than 'tell me about' (which is the single quick briefing)."""
    t = _strip_punct(text).strip()
    m = _DEEP_RE.match(t)
    if not m:
        return None
    subj = m.group(1).strip()
    if not subj or _PERSONAL.search(subj[:40]):
        return None
    return {"query": subj}


_RESEARCH_RE = re.compile(
    r"^(?:tell me (?:more )?about|who (?:is|was|are|were)|what (?:is|are|was|were)|"
    r"what'?s|describe|give me (?:a )?(?:rundown|summary|briefing|overview)\s+(?:of|on))\b\s+(.+)",
    re.I)


_HUD_VIEW_RE = re.compile(
    r"\b(?:make|create|build|show me|bring up|pull up|give me|open|generate|put up)\b"
    r".*\b(?:hud|view|dashboard|card|window|panel|display|overview)\b\s*"
    r"(?:of|for|about|on|showing|with)\s+(.+)", re.I)


def match_hud_view(text: str) -> dict | None:
    """'make a HUD/view/dashboard of X' -> display X (a research card). JARVIS
    CAN make HUD views — this stops such requests being mis-handed to Claude Code."""
    t = _strip_punct(text).strip()
    m = _HUD_VIEW_RE.search(t)
    if not m:
        return None
    subj = m.group(1).strip()
    if not subj or _PERSONAL.search(subj[:40]):
        return None
    return {"query": subj}


def match_research(text: str) -> dict | None:
    """Rich 'tell me about X' / 'who is X' / 'what is X' -> Haiku-written briefing
    (the local Wikipedia/DDG lookup grabbed wrong entities and thin snippets).
    Runs AFTER the specific tool matchers, so 'what is the weather' still routes
    to weather. Skips personal-context ('what's my name')."""
    t = _strip_punct(text).strip()
    m = _RESEARCH_RE.match(t)
    if not m:
        return None
    subj = m.group(1).strip()
    if not subj or _PERSONAL.search(subj[:40]):
        return None
    return {"query": subj}


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

# Order is significant: more-specific tools before the lookup catchall.
# file_read comes BEFORE scrape_url so "the file called groceries.txt"
# doesn't get parsed as a URL just because it contains a dot.
_PIPELINE: list[tuple[str, callable]] = [
    ("read_file",       match_file_read),
    ("list_files",      match_file_list),
    ("scrape_url",      match_scrape),
    ("open_chat",       match_open_chat),
    ("play_game",       match_game),
    ("screenshot",      match_screenshot),
    ("search_images",   match_search_images),
    ("calculate",       match_calculate),
    ("get_weather",     match_weather),
    ("get_news",        match_news),
    ("get_system_info", match_system),
    ("add_note",        match_note_add),
    ("list_notes",      match_note_list),
    ("search_notes",    match_note_search),
    ("get_stock",       match_stock),
    ("graph",           match_graph),
    ("deep_research",   match_deep_research),
    ("research",        match_hud_view),
    ("show_subject",    match_show_more),
    ("research",        match_research),
    ("research",        match_lookup),
]


def route(text: str) -> Intent | None:
    """Classify a transcript into a deterministic tool intent, or None.

    None means "no clear intent" — caller should fall back to free
    conversation (or the LLM tool-call path) for ambiguous queries.
    """
    if not text or not text.strip():
        return None
    for tool, matcher in _PIPELINE:
        args = matcher(text)
        if args is not None:
            return Intent(tool=tool, args=args)
    return None
