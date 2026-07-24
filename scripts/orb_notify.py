"""Helper script: send a notification to the user's phone via the Orb backend.

Usage:
    python orb_notify.py "Your message here"
    python orb_notify.py "Training hit 75%" --title "MyProject" --kind training
    python orb_notify.py "Need permission to delete old checkpoints" --kind permission

Any background Claude Code session can call this to reach the user even when
they're away from the PC. The Orb backend must be running (port 8340).
"""
import sys, os, json, pathlib, urllib.request, urllib.error, argparse


def _token() -> str:
    """ORB_TOKEN (or legacy JARVIS_TOKEN) from the environment, else from the
    repo .env — so this keeps working when the backend's HTTP token gate
    (ORB_HTTP_AUTH) is on."""
    tok = os.environ.get("ORB_TOKEN", "") or os.environ.get("JARVIS_TOKEN", "")
    if tok:
        return tok
    try:
        env = pathlib.Path(__file__).resolve().parent.parent / ".env"
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(("ORB_TOKEN=", "JARVIS_TOKEN=")):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def main():
    p = argparse.ArgumentParser(description="Send an Orb push notification")
    p.add_argument("body", help="Notification message (1 sentence)")
    p.add_argument("--title", default="Orb", help="Notification title")
    p.add_argument("--kind",  default="",    help="Routing hint: training|permission|alert|info|show")
    p.add_argument("--port",  default=8340, type=int, help="Orb backend port")
    args = p.parse_args()

    payload = json.dumps({
        "title": args.title,
        "body":  args.body,
        "kind":  args.kind or None,
    }).encode()

    url = f"http://localhost:{args.port}/api/notify"
    headers = {"Content-Type": "application/json"}
    tok = _token()
    if tok:
        headers["X-Orb-Token"] = tok
    req = urllib.request.Request(url, data=payload, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            result = json.loads(r.read())
            print(result.get("result", "Sent."))
    except urllib.error.URLError as e:
        print(f"Orb backend unreachable ({e}). Is it running on port {args.port}?",
              file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
