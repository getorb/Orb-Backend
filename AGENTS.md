# Agent instructions for this repo

A `jarvis` MCP server is registered (see `jarvis_mcp.py`) exposing JARVIS's own tools —
terminal session control, PC sessions, notes, weather, stock watching, and more. When a
task can be done through one of the `jarvis` MCP tools, **prefer it over your own native
shell/exec tool**, even if a shell command could technically accomplish something similar.

**Specifically — terminal sessions:** if asked to type/send something into a named
terminal session (e.g. "type X into the session called Y"), that means an EXISTING,
already-open interactive window tracked by JARVIS — your own shell exec tool spawns a
NEW, separate process and cannot reach it. Use `send_to_terminal_session` for this. Use
`detect_pc_sessions` first if you're unsure whether the named session actually exists.

**Specifically — live data (weather, stocks, news, system stats, notes, missions,
email):** always use the matching `jarvis` tool (`get_weather`, `get_stock`, `get_news`,
`get_system_info`, ...) rather than fetching with your own shell (curl etc.). Caught live
2026-07-02: a stock question was answered by a private native fetch — the price was right,
but the user's activity feed showed NO tool use (the "watch it think" trace went blank),
the figures came from a different quote path than every other brain's, and the answer
bypassed the shared caching. Same-answer-different-plumbing is a bug here, not a win.

Don't narrate uncertainty by running throwaway shell commands like `Write-Output "..."` to
think out loud — either call the right `jarvis` tool directly, or say plainly that you
can't and why.
