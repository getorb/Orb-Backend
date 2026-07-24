"""Gmail read tools — the off-device half of mail awareness (read-only v1).

Job-application replies were the motivating case: the proactive synthesis
should notice "something arrived worth knowing about" without the phone.
Uses a Gmail app password over IMAP (no OAuth); dormant until GMAIL_ADDRESS +
GMAIL_APP_PASSWORD exist in .env. Sending stays out of v1 on purpose —
reading can't do harm, sending can.
"""

import asyncio
import email
import email.header
import imaplib
import logging
import os

from tool_registry import tool

log = logging.getLogger("orb.tools.mail")

IMAP_HOST = "imap.gmail.com"

NOT_CONFIGURED = (
    "Email isn't set up yet — add GMAIL_APP_PASSWORD to .env and restart. "
    "(Google Account → Security → 2-Step Verification → App passwords.)"
)


def configured() -> bool:
    return bool(os.getenv("GMAIL_ADDRESS", "").strip() and os.getenv("GMAIL_APP_PASSWORD", "").strip())


def _connect() -> imaplib.IMAP4_SSL:
    addr = os.getenv("GMAIL_ADDRESS", "").strip()
    pw = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    m = imaplib.IMAP4_SSL(IMAP_HOST)
    m.login(addr, pw)
    return m


def _decode(raw: str) -> str:
    """RFC2047 header decode → plain text."""
    try:
        parts = email.header.decode_header(raw or "")
        return "".join(
            p.decode(enc or "utf-8", "replace") if isinstance(p, bytes) else p
            for p, enc in parts
        ).strip()
    except Exception:
        return (raw or "").strip()


def _headers_for(m: imaplib.IMAP4_SSL, mid: bytes) -> dict:
    typ, md = m.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
    raw = b""
    for part in md or []:
        if isinstance(part, tuple) and len(part) > 1:
            raw = part[1]
            break
    msg = email.message_from_bytes(raw)
    return {
        "id": mid.decode(),
        "from": _decode(msg.get("From", ""))[:80],
        "subject": _decode(msg.get("Subject", ""))[:120] or "(no subject)",
        "date": _decode(msg.get("Date", ""))[:31],
    }


def _search_sync(criteria: list, limit: int) -> list[dict]:
    """Run one IMAP search and return newest-first header summaries."""
    m = _connect()
    try:
        m.select("INBOX", readonly=True)
        typ, data = m.search(None, *criteria)
        ids = (data[0] or b"").split()
        return [_headers_for(m, mid) for mid in ids[-max(1, limit):][::-1]]
    finally:
        try:
            m.logout()
        except Exception:
            pass


def primary_recent(limit: int = 5, days: int = 1) -> list[dict]:
    """Real mail only — Gmail's Primary category, newest first. In a many-
    thousand-unread inbox, 'unread' is no signal; Google's category ML is."""
    return _search_sync(["X-GM-RAW", f'"category:primary newer_than:{days}d"'], limit)


def job_signals(limit: int = 5, days: int = 2) -> list[dict]:
    """Job-hunt-relevant mail — the motivating case for this whole module."""
    # No inner quotes — the IMAP literal is already double-quoted, and nesting
    # breaks the SEARCH parser (hit live on first run, 2026-07-02).
    q = (f"newer_than:{days}d (recruiter OR interview OR application OR offer "
         f"OR position OR hiring)")
    return _search_sync(["X-GM-RAW", f'"{q}"'], limit)


def unread_summaries(limit: int = 6) -> tuple[int, list[dict]]:
    """(total unread count, newest-first summaries) — sync; callers thread it."""
    m = _connect()
    try:
        m.select("INBOX", readonly=True)
        typ, data = m.search(None, "UNSEEN")
        ids = (data[0] or b"").split()
        return len(ids), [_headers_for(m, mid) for mid in ids[-max(1, limit):][::-1]]
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _body_sync(msg_id: str, max_chars: int = 1400) -> str:
    m = _connect()
    try:
        m.select("INBOX", readonly=True)
        typ, md = m.fetch(msg_id.encode(), "(BODY.PEEK[])")
        raw = b""
        for part in md or []:
            if isinstance(part, tuple) and len(part) > 1:
                raw = part[1]
                break
        msg = email.message_from_bytes(raw)
        head = f"From: {_decode(msg.get('From',''))}\nSubject: {_decode(msg.get('Subject',''))}\nDate: {_decode(msg.get('Date',''))}\n\n"
        body = ""
        parts = list(msg.walk()) if msg.is_multipart() else [msg]
        for p in parts:
            if p.get_content_type() == "text/plain":
                try:
                    body = p.get_payload(decode=True).decode(
                        p.get_content_charset() or "utf-8", "replace")
                    break
                except Exception:
                    continue
        if not body.strip():
            # Most corporate mail is HTML-only (hit live on the very first read,
            # e.g. a recruiter reply from Acme Corp). Strip it to readable text.
            import html as _html
            import re as _re
            for p in parts:
                if p.get_content_type() == "text/html":
                    try:
                        raw = p.get_payload(decode=True).decode(
                            p.get_content_charset() or "utf-8", "replace")
                    except Exception:
                        continue
                    raw = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
                    raw = _re.sub(r"(?i)</(p|div|tr|li|br)[^>]*>", "\n", raw)
                    body = _html.unescape(_re.sub(r"<[^>]+>", " ", raw))
                    break
        body = " ".join((body or "(no readable body)").split())
        return head + body[:max_chars]
    finally:
        try:
            m.logout()
        except Exception:
            pass


@tool(
    name="check_email",
    description=(
        "Check the user's Gmail inbox for unread mail. Use when they ask 'any "
        "new email', 'did anyone reply', 'check my inbox', or when a job-"
        "application reply might have arrived."
    ),
    parameters={
        "limit": {"type": "integer",
                  "description": "How many unread messages to summarize (default 5, max 10)."},
    },
    required=[],
)
async def check_email(limit: int = 5) -> str:
    if not configured():
        return NOT_CONFIGURED
    limit = max(1, min(int(limit or 5), 10))
    try:
        total, rows = await asyncio.to_thread(unread_summaries, limit)
    except Exception as e:
        return f"Couldn't reach Gmail — {str(e)[:120]}"
    if not total:
        return "Inbox is clear — nothing unread."
    lines = "; ".join(f"{r['from']} — {r['subject']}" for r in rows)
    more = f" (+{total - len(rows)} more)" if total > len(rows) else ""
    return f"{total} unread. Latest: {lines}{more}."


@tool(
    name="search_email",
    description=(
        "Search the user's Gmail with full Gmail search syntax (e.g. "
        "'from:recruiter newer_than:7d', 'subject:interview'). Use for 'find "
        "the email about X', 'did Acme Corp ever reply'."
    ),
    parameters={
        "query": {"type": "string", "description": "Gmail search query."},
        "limit": {"type": "integer", "description": "Max results (default 5, max 10)."},
    },
    required=["query"],
)
async def search_email(query: str, limit: int = 5) -> str:
    if not configured():
        return NOT_CONFIGURED
    q = (query or "").strip()
    if not q:
        return "What should I search for?"
    limit = max(1, min(int(limit or 5), 10))
    try:
        rows = await asyncio.to_thread(_search_sync, ["X-GM-RAW", f'"{q}"'], limit)
    except Exception as e:
        return f"Couldn't search Gmail — {str(e)[:120]}"
    if not rows:
        return f"Nothing in the inbox matches '{q}'."
    lines = "; ".join(f"[{r['id']}] {r['from']} — {r['subject']} ({r['date']})" for r in rows)
    return f"Found {len(rows)}: {lines}."


@tool(
    name="read_email",
    description=(
        "Read one email's content by the id number that check_email/search_email "
        "returned in [brackets]. Use when the user wants what a message actually says."
    ),
    parameters={
        "message_id": {"type": "string", "description": "The message id from a prior search."},
    },
    required=["message_id"],
)
async def read_email(message_id: str) -> str:
    if not configured():
        return NOT_CONFIGURED
    mid = (message_id or "").strip()
    if not mid.isdigit():
        return "I need the numeric id from a prior check_email/search_email result."
    try:
        return await asyncio.to_thread(_body_sync, mid)
    except Exception as e:
        return f"Couldn't fetch that message — {str(e)[:120]}"
