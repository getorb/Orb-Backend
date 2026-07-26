"""Factual lookup tool — Wikipedia first, then DuckDuckGo."""

from tool_registry import tool
from tools import web_search


@tool(
    name="lookup_fact",
    description=(
        "Look up a factual answer about a person, place, event, definition, "
        "or topic. Use this when the user asks 'who/what/when/where is X', "
        "'tell me about X', or any question whose answer isn't already in "
        "your memory. Returns 1-2 sentences from Wikipedia or DuckDuckGo. "
        "Do NOT use for math, weather, system stats, or news headlines."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "The thing to look up. Strip filler words: 'who is Nikola Tesla' -> 'Nikola Tesla'.",
        },
    },
    required=["query"],
)
async def lookup_fact(query: str) -> str:
    result = await web_search(query)
    return result or f"I couldn't find anything reliable about {query}."
