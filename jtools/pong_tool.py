"""Pong game — 'I'm bored' / 'play pong' trigger."""

from tool_registry import tool


@tool(
    name="play_pong",
    description=(
        "Launch a game of Pong against J.A.R.V.I.S on the phone. "
        "Use when the user is bored, wants to play a game, or asks to play pong. "
        "Pairs well with 'I'm bored' or 'entertain me'."
    ),
    parameters={},
    required=[],
)
async def play_pong() -> str:
    # server_win.py intercepts "play_game" intent and also handles play_pong
    # by returning the game file path — the server's on_tool checks for this.
    return "pong.html"
