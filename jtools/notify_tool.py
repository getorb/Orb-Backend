from tool_registry import tool

# Injected by server_win lifespan — async fn(title, body, kind?) -> str
_deliver_fn = None


@tool(
    name="send_notification",
    description=(
        "Send a push notification to the user's phone. Use when something important "
        "happens that the user should know about even if they're away — a task finished, "
        "a training run hit a milestone, a permission request from another process, or "
        "any news worth surfacing proactively. Provide a short title and a 1-sentence body."
    ),
    parameters={
        "title": {"type": "string", "description": "Notification title (short, ~5 words)."},
        "body":  {"type": "string", "description": "Notification body (one sentence)."},
        "kind":  {"type": "string", "description": "Optional routing hint for the app tap (e.g. 'training', 'permission', 'alert')."},
    },
    required=["title", "body"],
)
async def send_notification(title: str, body: str, kind: str = "") -> str:
    if _deliver_fn is None:
        return "Notification system not initialized."
    return await _deliver_fn(title, body, kind or None)
