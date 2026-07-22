"""apns.py — Apple Push Notification (APNs) provider for the PC / hosted "remote brain" tier.

The PC tier reaches the user when the Orb app is CLOSED via APNs — the only iOS-sanctioned
always-on path (no persistent background sockets). The phone registers for remote
notifications and sends its device token in the WS `hello`; the backend stores it here and,
when the proactivity engine has something worth surfacing, signs an ES256 JWT and POSTs an
alert to Apple over HTTP/2.

Dependencies (SENDING only — token storage uses just the stdlib, so the WS `hello` handler
can `register_token` even before these are installed):
    pip install 'httpx[http2]' 'pyjwt[crypto]'

Config (env / .env), set on the PC:
    APNS_KEY_PATH   path to the AuthKey_XXXX.p8 (one key works for BOTH environments)
    APNS_KEY_ID     the key id (Apple Developer -> Keys)
    APNS_TEAM_ID    your Apple team id
    APNS_BUNDLE_ID  push topic — YOUR app's bundle id (required to send; no default)
    APNS_ENV        prod | sandbox | auto  (default auto: try prod, fall back to sandbox)

Environment trap (PC_TIER_PLAN §7): an Xcode *debug* build gets a SANDBOX token
(api.sandbox.push.apple.com); TestFlight & the App Store use PRODUCTION
(api.push.apple.com) even though TestFlight is "beta." `auto` tries prod first, then
sandbox on a BadDeviceToken/wrong-environment reason.

Team/bundle coherence: Apple delivers a push only if the signing key's TEAM owns the
target app's APNS_BUNDLE_ID. A self-hoster's own key therefore CANNOT push to the public
App Store Orb app (signed under the owner's team) — only to an iOS build they sign under
their own team + bundle id. Without that they still get in-app WebSocket notifications
while the app is open (JARVIS_WS_NOTIFICATIONS); closed-app push needs their own build.

Privacy: nothing sensitive in plaintext — the payload transits Apple's servers. Put a
`kind` in the payload so the phone's notification-tap routing can open the right surface
(mirrors the existing local-notification tap routing).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("jarvis.apns")

BUNDLE_ID = os.getenv("APNS_BUNDLE_ID", "")  # your app's bundle id — no default (see .env)
KEY_ID = os.getenv("APNS_KEY_ID", "")
TEAM_ID = os.getenv("APNS_TEAM_ID", "")
KEY_PATH = os.getenv("APNS_KEY_PATH", "")
ENV = os.getenv("APNS_ENV", "auto").lower()

_PROD_HOST = "api.push.apple.com"
_SANDBOX_HOST = "api.sandbox.push.apple.com"

# Rejection reasons that blame the DEVICE TOKEN itself (vs the key/config):
# only when every host tried answers with one of these is a token provably dead.
_TOKEN_FAULT_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic",
                        "ExpiredToken"}

# Device tokens are non-secret but personal — keep them out of git (see .gitignore).
_TOKENS_FILE = Path(__file__).parent / "data" / "apns_tokens.json"
_tokens: set[str] | None = None


# --- token registry (stdlib only; safe to call from the WS `hello` handler) ----------

def _load() -> set[str]:
    global _tokens
    if _tokens is None:
        try:
            _tokens = set(json.loads(_TOKENS_FILE.read_text()))
        except Exception:
            _tokens = set()
    return _tokens


def register_token(token: str) -> None:
    """Remember a phone's APNs device token (called from the WS `hello`). Idempotent."""
    token = (token or "").strip()
    if not token:
        return
    toks = _load()
    if token not in toks:
        toks.add(token)
        try:
            _TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _TOKENS_FILE.write_text(json.dumps(sorted(toks)))
        except Exception as e:
            log.debug(f"[apns] token persist skipped: {e}")
    log.info(f"[apns] token registered ({len(token)} chars); {len(toks)} known")


def known_tokens() -> list[str]:
    """Every device we can push to (today: just ninjahawk's phone)."""
    return sorted(_load())


def unregister_token(token: str) -> None:
    """Drop a token every environment has declared dead, so one stale token from an
    old install doesn't burn two APNs round-trips on every push forever."""
    toks = _load()
    if token in toks:
        toks.discard(token)
        try:
            _TOKENS_FILE.write_text(json.dumps(sorted(toks)))
        except Exception:
            pass
        log.info(f"[apns] pruned dead token {token[:12]}...; {len(toks)} left")


def configured() -> bool:
    """True when the signing creds are present, so callers can no-op cleanly otherwise."""
    return bool(KEY_PATH and KEY_ID and TEAM_ID)


# --- provider JWT (cached; APNs allows reuse up to 60 min, rejects a regen < 20 min) --

_jwt_cache: tuple[str, float] | None = None


def _provider_jwt() -> str:
    global _jwt_cache
    now = time.time()
    if _jwt_cache and now - _jwt_cache[1] < 50 * 60:
        return _jwt_cache[0]
    if not configured():
        raise RuntimeError("APNs not configured: set APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID")
    try:
        import jwt  # PyJWT (needs the [crypto] extra for ES256)
    except ImportError as e:
        raise RuntimeError("APNs send needs PyJWT: pip install 'pyjwt[crypto]'") from e
    key = Path(KEY_PATH).read_text()
    token = jwt.encode({"iss": TEAM_ID, "iat": int(now)}, key,
                       algorithm="ES256", headers={"kid": KEY_ID})
    _jwt_cache = (token, now)
    return token


def _hosts() -> list[str]:
    if ENV == "prod":
        return [_PROD_HOST]
    if ENV == "sandbox":
        return [_SANDBOX_HOST]
    return [_PROD_HOST, _SANDBOX_HOST]   # auto: prod first, fall back to sandbox


# --- send -----------------------------------------------------------------------------

async def send(token: str, title: str = "", body: str = "", *,
               kind: str | None = None, badge: int | None = None,
               sound: str = "default", extra: dict | None = None) -> bool:
    """Push one alert to one device. Returns True on an APNs 200. `kind` routes the tap on
    the phone. Keep `title`/`body` non-sensitive (the payload transits Apple)."""
    try:
        import httpx
    except ImportError as e:
        raise RuntimeError("APNs send needs httpx: pip install 'httpx[http2]'") from e
    if not BUNDLE_ID:
        log.warning("[apns] APNS_BUNDLE_ID not set — cannot push; set it in .env to your "
                    "app's bundle id")
        return False
    alert: dict = {}
    if title:
        alert["title"] = title
    if body:
        alert["body"] = body
    aps: dict = {"alert": alert, "sound": sound,
                 "thread-id": "orb"}  # one stack in iOS notification center
    if badge is not None:
        aps["badge"] = badge
    payload: dict = {"aps": aps}
    if kind:
        payload["kind"] = kind
    if extra:
        payload.update(extra)
    headers = {
        "authorization": f"bearer {_provider_jwt()}",
        "apns-topic": BUNDLE_ID,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }
    content = json.dumps(payload).encode()
    hosts = _hosts()
    last_reason = ""
    reasons: list[str] = []
    async with httpx.AsyncClient(http2=True, timeout=10.0) as client:
        for i, host in enumerate(hosts):
            try:
                r = await client.post(f"https://{host}/3/device/{token}",
                                      content=content, headers=headers)
            except Exception as e:
                log.warning(f"[apns] POST {host} failed: {e}")
                last_reason = str(e)
                reasons.append("")   # network trouble — says nothing about the token
                continue
            if r.status_code == 200:
                return True
            try:
                reason = r.json().get("reason", "")
            except Exception:
                reason = (r.text or "")[:120]
            last_reason = f"{r.status_code} {reason}"
            reasons.append(reason)
            # In `auto` mode, ANY rejection might mean this token belongs to the
            # OTHER environment (e.g. BadDeviceToken, DeviceTokenNotForTopic,
            # BadEnvironmentKeyInToken) — try the remaining host(s) before giving
            # up rather than trusting a hardcoded reason whitelist.
            if i < len(hosts) - 1:
                log.info(f"[apns] {host} -> {reason}; trying the other environment")
                continue
    log.warning(f"[apns] all hosts rejected token {token[:12]}...: {last_reason}")
    # Prune ONLY when every environment blamed the token itself. A key-side
    # rejection (e.g. BadEnvironmentKeyInToken = the .p8 is environment-restricted
    # on the Apple portal) must never empty the registry — those tokens may be
    # fine the moment the key is fixed.
    if reasons and all(rn in _TOKEN_FAULT_REASONS for rn in reasons):
        unregister_token(token)
    return False


async def broadcast(title: str = "", body: str = "", **kw) -> int:
    """Push to every known device. Returns how many were delivered."""
    sent = 0
    for t in known_tokens():
        try:
            if await send(t, title, body, **kw):
                sent += 1
        except Exception as e:
            log.warning(f"[apns] broadcast to one token failed: {e}")
    return sent
