"""Weather tool — wraps the existing open-meteo implementation."""

from tool_registry import tool
from tools import get_weather as _get_weather
from jtools.cache_util import ttl_cache


@tool(
    name="get_weather",
    description=(
        "Get the current weather or short forecast. Use this whenever the user asks "
        "about weather, temperature, rain, forecast, or 'how hot/cold' somewhere is. "
        "If the user names a place, pass it; if they DON'T name one (e.g. 'what's the "
        "weather?'), OMIT location — it defaults to the user's home location. Never "
        "refuse for lack of a location; just call it. Returns a one-sentence summary."
    ),
    parameters={
        "location": {
            "type": "string",
            "description": "Optional city, state, or place (e.g. 'Tucson', 'Tokyo'). Omit to use the user's home location.",
        },
        "tomorrow": {
            "type": "boolean",
            "description": "True if the user asked about tomorrow's weather; otherwise false.",
        },
    },
    required=[],
)
@ttl_cache(600)  # weather doesn't meaningfully change minute to minute
async def get_weather(location: str | None = None, tomorrow: bool = False) -> str:
    # location is OPTIONAL: tools.get_weather() defaults a missing/blank location
    # to DEFAULT_LOCATION (same fallback the weather CARD uses), so the spoken
    # answer and the card agree instead of contradicting each other.
    return await _get_weather((location or "").strip() or None, tomorrow=tomorrow)
