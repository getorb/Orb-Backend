from tool_registry import tool
import asyncio, os, shutil

_DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")


@tool(
    name="run_claude_code",
    description=(
        "Build software, write or edit files, run code, or perform any agentic task that "
        "requires reading/writing the filesystem. Use for: 'build me X', 'create a Y', "
        "'write a script', 'fix this code', 'make me a Z'. Pass the full task description "
        "and any relevant context from the conversation."
    ),
    parameters={
        "task": {"type": "string", "description": "Full description of what to build or do."},
        "context": {"type": "string", "description": "Relevant conversation context (optional)."},
    },
    required=["task"],
)
async def run_claude_code(task: str, context: str = "") -> str:
    if not shutil.which("claude"):
        return "Claude Code CLI not available."
    prompt = f"{context}\n\nTask: {task}".strip() if context else task
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--permission-mode", "bypassPermissions",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=_DESKTOP,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 120.0)
        text = out.decode("utf-8", "ignore").strip()
        return text[:1000] if text else "Done — no output returned."
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        return "Task is still running — I'll let you know when it finishes."
    except Exception as e:
        return f"Error: {e}"
