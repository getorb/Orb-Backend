"""Orb Backend guided setup — token, .env, push, boot check, pairing.

Run:  python scripts/setup.py            (interactive)
      python scripts/setup.py --check    (non-interactive environment check)

Same flow Claude Code walks through when you say "set up the Orb backend for me"
(see CLAUDE.md). No secrets ever leave this machine; everything lands in .env.
"""
import argparse
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Windows consoles default to cp1252 and choke on the check marks below.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
APNS_KEYS_URL = "https://developer.apple.com/account/resources/authkeys/list"


def say(msg: str) -> None:
    print(f"\n{msg}")


def qr(data: str, label: str) -> None:
    """Print a terminal QR when the qrcode lib is present; always print the text."""
    print(f"  {label}: {data}")
    try:
        import qrcode  # type: ignore
        q = qrcode.QRCode(border=1)
        q.add_data(data)
        q.print_ascii(invert=True)
    except Exception:
        print("  (pip install qrcode for a scannable code here)")


def read_env() -> dict:
    vals = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def write_env_value(key: str, value: str) -> None:
    """Set key=value in .env, replacing an existing line or appending."""
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#"):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")


def check(interactive: bool) -> bool:
    ok = True
    if sys.version_info < (3, 11):
        print(f"✗ Python 3.11+ required (you have {sys.version.split()[0]})")
        ok = False
    else:
        print(f"✓ Python {sys.version.split()[0]}")
    for mod in ("fastapi", "uvicorn", "httpx", "yaml", "mcp"):
        try:
            __import__(mod)
            print(f"✓ {mod}")
        except ImportError:
            print(f"✗ {mod} missing — pip install -r requirements.txt")
            ok = False
    if shutil.which("claude"):
        print("✓ claude CLI on PATH (default brain available)")
    else:
        print("– claude CLI not found: install Claude Code for the default brain "
              "(npm install -g @anthropic-ai/claude-code), or configure "
              "JARVIS_FREE_BACKENDS / a local model")
    if shutil.which("tailscale"):
        print("✓ tailscale on PATH (recommended transport)")
    else:
        print("– tailscale not found (recommended: tailscale.com — reach your "
              "backend from anywhere)")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="environment check only")
    args = ap.parse_args()

    say("Orb Backend setup")
    ok = check(interactive=not args.check)
    if args.check:
        return 0 if ok else 1

    # 1. .env baseline
    if not ENV.exists():
        if ENV_EXAMPLE.exists():
            ENV.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            say("Created .env from .env.example")
        else:
            ENV.write_text("", encoding="utf-8")
            say("Created empty .env")

    # 2. Token (ORB_TOKEN is the documented name; legacy JARVIS_TOKEN still works)
    vals = read_env()
    port = vals.get("ORB_PORT") or vals.get("JARVIS_PORT") or "8340"
    if vals.get("ORB_TOKEN") or vals.get("JARVIS_TOKEN"):
        say("Pairing token already set — keeping it.")
        token = vals.get("ORB_TOKEN") or vals["JARVIS_TOKEN"]
    else:
        token = secrets.token_hex(32)
        write_env_value("ORB_TOKEN", token)
        say("Generated ORB_TOKEN and wrote it to .env")

    # 3. Identity
    name = input("\nWhat should Orb call you? (blank to skip): ").strip()
    if name:
        write_env_value("USER_NAME", name)
    city = input("Your city, for weather defaults (blank to skip): ").strip()
    if city:
        write_env_value("ORB_DEFAULT_LOCATION", city)

    # 4. Push
    if input("\nConfigure push notifications now? Needs a paid Apple Developer "
             "account. [y/N]: ").strip().lower() == "y":
        say("Create an APNs key (Keys -> +, enable Apple Push Notifications service),")
        print("download the .p8 (Apple only offers it once), and put it in the repo root.")
        qr(APNS_KEYS_URL, "Apple Developer keys page")
        p8 = input("Path/filename of your .p8 (e.g. AuthKey_ABC123.p8): ").strip()
        kid = input("Key ID: ").strip()
        team = input("Team ID: ").strip()
        bundle = input("App bundle ID: ").strip()
        for k, v in (("APNS_KEY_PATH", p8), ("APNS_KEY_ID", kid),
                     ("APNS_TEAM_ID", team), ("APNS_BUNDLE_ID", bundle),
                     ("APNS_ENV", "auto")):
            if v:
                write_env_value(k, v)
        say("APNs settings written.")
    else:
        say("Skipping push — everything else works; add APNs to .env any time.")

    # 5. Boot check
    if input("\nBoot the server now to verify? [Y/n]: ").strip().lower() != "n":
        py = ROOT / "venv" / "Scripts" / "python.exe"
        py = str(py) if py.exists() else sys.executable
        proc = subprocess.Popen([py, str(ROOT / "server_win.py")], cwd=str(ROOT))
        say("Starting server_win.py … (first run downloads the Whisper model)")
        healthy = False
        for _ in range(60):
            time.sleep(2)
            try:
                with urllib.request.urlopen(f"http://localhost:{port}/api/health",
                                            timeout=3) as r:
                    if r.status == 200:
                        healthy = True
                        break
            except Exception:
                pass
        print("✓ /api/health answered" if healthy
              else "✗ health check didn't answer in 120s — see backend_err.log")
        proc.terminate()
        say("Stopped the test server. For real use, run:  python supervisor.py")

    # 6. Pairing
    say("Pairing: in the Orb app -> Settings -> Your Server, enter your backend URL "
        "and this token.")
    print(f"  If you use Tailscale:  tailscale funnel {port}   (public URL)")
    print(f"                         tailscale serve {port}    (your devices only)")
    print(f"  Token (also in .env): {token[:8]}…{token[-4:]}")
    if input("Show the full token as a QR for easy phone entry? [y/N]: ").strip().lower() == "y":
        qr(token, "ORB_TOKEN")
    say("Done. Full manual reference: SELF_HOSTING.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
