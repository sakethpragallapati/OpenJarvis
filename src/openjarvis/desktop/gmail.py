"""Gmail MCP server (FastMCP + the official Gmail API).

Tools (mounted into the desktop server as ``gmail_*``, or served on their
own with ``jarvis desktop gmail-mcp``):

- ``unread``  - newest unread inbox mail (sender + subject)
- ``search``  - Gmail search syntax ("from:priya newer_than:7d")
- ``read``    - one message's text
- ``draft``   - save a draft (no typing into the browser)
- ``send``    - send an email (destructive: the agent confirms out loud,
  reading back the real address)

Names instead of addresses ("email Ravi") are resolved from your own recent
mail headers; nothing is sent to an address you didn't hear first.

One-time setup (your own Google OAuth client, so the app is yours):
``jarvis desktop gmail-auth --credentials <client_secret.json>``.
"""

from __future__ import annotations

import base64
import html
import re
import threading
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Tuple

from pydantic import Field

from openjarvis.desktop.winapi import DesktopError

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")


class GmailError(DesktopError):
    """User-facing Gmail failure."""


def gmail_dir() -> Path:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / "gmail"


def credentials_path() -> Path:
    return gmail_dir() / "credentials.json"


def token_path() -> Path:
    return gmail_dir() / "token.json"


def is_connected() -> bool:
    return token_path().exists()


def gmail_available() -> bool:
    import importlib.util

    return all(
        importlib.util.find_spec(m) is not None
        for m in ("googleapiclient", "google_auth_oauthlib")
    )


def authorize(client_file: Optional[Path] = None) -> str:
    """Run Google's consent flow in the browser; returns the account address."""
    import shutil

    from google_auth_oauthlib.flow import InstalledAppFlow

    gmail_dir().mkdir(parents=True, exist_ok=True)
    if client_file is not None:
        shutil.copyfile(client_file, credentials_path())
    if not credentials_path().exists():
        raise GmailError(
            f"No OAuth client file. Download one (Desktop app) from Google Cloud "
            f"Console and pass --credentials, or save it as {credentials_path()}."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path()), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    token_path().write_text(creds.to_json(), encoding="utf-8")
    _SERVICE.clear()
    return profile_address()


_SERVICE: Dict[str, Any] = {}
_LOCK = threading.Lock()


def service() -> Any:
    """The Gmail API client (token refreshed and saved as needed)."""
    with _LOCK:
        if "svc" in _SERVICE:
            return _SERVICE["svc"]
        if not is_connected():
            raise GmailError(
                "Gmail isn't connected yet. Run `jarvis desktop gmail-auth` once."
            )
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(token_path()), SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    raise GmailError(
                        "Gmail sign-in expired. Run `jarvis desktop gmail-auth` again."
                    ) from exc
                token_path().write_text(creds.to_json(), encoding="utf-8")
            else:
                raise GmailError(
                    "Gmail sign-in is invalid; run `jarvis desktop gmail-auth`."
                )
        _SERVICE["svc"] = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return _SERVICE["svc"]


def _svc(svc: Any) -> Any:
    return svc if svc is not None else service()


def profile_address(svc: Any = None) -> str:
    return _svc(svc).users().getProfile(userId="me").execute().get("emailAddress", "")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _headers(message: Dict[str, Any]) -> Dict[str, str]:
    return {
        h.get("name", "").lower(): h.get("value", "")
        for h in message.get("payload", {}).get("headers", [])
    }


def _sender(value: str) -> str:
    name, address = parseaddr(value)
    return name or address


def _list(svc: Any, query: str, limit: int) -> List[Dict[str, Any]]:
    users = _svc(svc).users()
    found = (
        users.messages()
        .list(userId="me", q=query, maxResults=max(1, min(int(limit), 20)))
        .execute()
        .get("messages", [])
    )
    out = []
    for item in found:
        out.append(
            users.messages()
            .get(
                userId="me",
                id=item["id"],
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            )
            .execute()
        )
    return out


# The last list shown (unread or search): (id, sender, subject). "Read the
# second one" / "read the OpenRouter one" pick from it, so the model never
# has to handle (or invent) Gmail message ids.
_LAST: List[Tuple[str, str, str]] = []


def _remember(messages: List[Dict[str, Any]]) -> None:
    global _LAST
    _LAST = []
    for message in messages:
        h = _headers(message)
        _LAST.append(
            (
                str(message.get("id", "")),
                _sender(h.get("from", "")),
                h.get("subject", "(no subject)"),
            )
        )


def unread(limit: int = 5, *, svc: Any = None) -> str:
    messages = _list(svc, "is:unread in:inbox category:primary", limit)
    if not messages:
        return "No unread emails in your Primary inbox."
    _remember(messages)
    lines = [
        f"{i}. {sender}: {subject}" for i, (_, sender, subject) in enumerate(_LAST, 1)
    ]
    return f"{len(lines)} unread emails:\n" + "\n".join(lines)


def search(query: str, limit: int = 5, *, svc: Any = None) -> str:
    messages = _list(svc, query, limit)
    if not messages:
        return f"No emails match {query!r}."
    _remember(messages)
    lines = []
    for i, m in enumerate(messages, 1):
        h = _headers(m)
        snippet = html.unescape(m.get("snippet", ""))[:120]
        lines.append(
            f"{i}. {_sender(h.get('from', ''))}: {h.get('subject', '(no subject)')} "
            f"({h.get('date', '')[:16]}) {snippet}"
        )
    return "\n".join(lines)


_ORDINAL = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "latest": 1,
}  # fmt: skip
_GMAIL_ID = re.compile(r"^[0-9a-f]{12,20}$")


def pick(which: str) -> str:
    """The message id for "2", "the second one", "OpenRouter", or an id."""
    text = " ".join((which or "").lower().split())
    if not text:
        raise ValueError("Say which email: its number in the list or the sender.")
    number = re.search(r"\b(\d{1,2})\b", text)
    word = next((v for k, v in _ORDINAL.items() if re.search(rf"\b{k}\b", text)), None)
    index = int(number.group(1)) if number else word
    if index is not None and _LAST:
        position = index - 1 if index > 0 else len(_LAST) - 1
        if 0 <= position < len(_LAST):
            return _LAST[position][0]
    for message_id, sender, subject in _LAST:
        if text == message_id:
            return message_id
    words = [w for w in re.findall(r"[a-z0-9]+", text) if w not in _ORDINAL]
    words = [
        w for w in words if w not in {"the", "email", "mail", "from", "about", "one"}
    ]
    if words:
        for message_id, sender, subject in _LAST:
            haystack = f"{sender} {subject}".lower()
            if all(w in haystack for w in words):
                return message_id
    if _GMAIL_ID.match(text):
        return text  # an id from somewhere else; the API will say if it's wrong
    listed = "; ".join(f"{i}. {s}" for i, (_, s, _) in enumerate(_LAST, 1))
    raise ValueError(
        "I don't know which email that is. "
        + (f"From the last list: {listed}." if listed else "Check your mail first.")
    )


def _body_text(payload: Dict[str, Any]) -> str:
    def decode(data: str) -> str:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", errors="replace"
        )

    plain, rich = [], []
    stack = [payload]
    while stack:
        part = stack.pop(0)
        stack.extend(part.get("parts", []) or [])
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        if part.get("mimeType") == "text/plain":
            plain.append(decode(data))
        elif part.get("mimeType") == "text/html":
            rich.append(decode(data))
    if plain:
        return "\n".join(plain)
    text = re.sub(r"<(script|style).*?</\1>", " ", "\n".join(rich), flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def read(message_id: str, *, svc: Any = None, limit: int = 3000) -> str:
    message_id = pick(message_id)
    try:
        message = (
            _svc(svc)
            .users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
    except Exception as exc:
        if "404" in str(exc) or "not found" in str(exc).lower():
            raise ValueError(
                "That email doesn't exist. Say its number in the last list or "
                "who it's from."
            ) from exc
        raise
    h = _headers(message)
    body = "\n".join(
        line.strip() for line in _body_text(message.get("payload", {})).splitlines()
    )
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) > limit:
        body = body[:limit] + "\n[... truncated]"
    return (
        f"From: {h.get('from', '')}\nSubject: {h.get('subject', '')}\n"
        f"Date: {h.get('date', '')}\n\n{body}"
    )


# ---------------------------------------------------------------------------
# Recipients, drafts, sending
# ---------------------------------------------------------------------------

_RESOLVED: Dict[str, str] = {}


def resolve(to: str, *, svc: Any = None) -> Tuple[List[str], List[str]]:
    """Addresses for 'ravi, a@b.com' - names looked up in your mail headers."""
    parts = [p.strip() for p in re.split(r"[,;]|\s+and\s+", to or "") if p.strip()]
    addresses: List[str] = []
    unknown: List[str] = []
    for part in parts:
        name, address = parseaddr(part)
        if _EMAIL.match(address or part):
            addresses.append(address or part)
            continue
        key = part.lower()
        if key not in _RESOLVED:
            found = _lookup(part, svc)
            if found:
                _RESOLVED[key] = found
        if key in _RESOLVED:
            addresses.append(_RESOLVED[key])
        else:
            unknown.append(part)
    return addresses, unknown


def _lookup(name: str, svc: Any) -> Optional[str]:
    words = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 1]
    if not words:
        return None
    query = f"(from:{words[0]} OR to:{words[0]}) newer_than:3y"
    counts: Dict[str, int] = {}
    for message in _list(svc, query, 15):
        h = _headers(message)
        for display, address in getaddresses([h.get("from", ""), h.get("to", "")]):
            haystack = f"{display} {address}".lower()
            if address and all(w in haystack for w in words):
                counts[address.lower()] = counts.get(address.lower(), 0) + 1
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    if len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
        return None  # ambiguous: two people, equally likely
    return ranked[0][0]


def _raw(to: List[str], subject: str, body: str) -> str:
    message = EmailMessage()
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def _recipients(to: str, svc: Any) -> List[str]:
    addresses, unknown = resolve(to, svc=svc)
    if unknown:
        raise GmailError(
            f"I don't know the email address for {', '.join(unknown)}. Say it, "
            "or tell me to remember it."
        )
    if not addresses:
        raise GmailError("Who should the email go to?")
    return addresses


def draft(to: str, subject: str, body: str, *, svc: Any = None) -> str:
    recipients = _recipients(to, _svc(svc))
    _svc(svc).users().drafts().create(
        userId="me", body={"message": {"raw": _raw(recipients, subject, body)}}
    ).execute()
    return (
        f"Saved a Gmail draft to {', '.join(recipients)}: {subject}. "
        "Say 'send it' to send it."
    )


def send(to: str, subject: str, body: str, *, svc: Any = None) -> str:
    if not body.strip():
        raise GmailError("The email is empty.")
    recipients = _recipients(to, _svc(svc))
    _svc(svc).users().messages().send(
        userId="me", body={"raw": _raw(recipients, subject, body)}
    ).execute()
    return f"Sent your email to {', '.join(recipients)}."


def describe_send(args: Dict[str, Any]) -> Optional[str]:
    """The confirmation to read out before sending, with real addresses.

    None when a recipient can't be resolved: then ``send`` fails safely
    without sending, and the model asks for the address instead.
    """
    try:
        addresses, unknown = resolve(str(args.get("to", "")))
    except Exception:
        return f"Send the email '{args.get('subject', '')}' to {args.get('to', '')}?"
    if unknown or not addresses:
        return None
    return f"Send the email '{args.get('subject', '')}' to {', '.join(addresses)}?"


# ---------------------------------------------------------------------------
# The MCP server
# ---------------------------------------------------------------------------


def build_gmail_server() -> Any:
    from fastmcp import FastMCP

    mcp = FastMCP(
        "jarvis-gmail",
        instructions="Read, search, draft and send email in the user's Gmail.",
    )

    @mcp.tool(name="unread", annotations={"readOnlyHint": True})
    def unread_tool(limit: int = 5) -> str:
        """Newest unread emails in the Primary inbox."""
        return unread(limit)

    @mcp.tool(name="search", annotations={"readOnlyHint": True})
    def search_tool(
        query: Annotated[
            str,
            Field(
                description="Gmail search built from the user's words, e.g. "
                "'from:<sender> newer_than:7d' or 'subject:<words>'."
            ),
        ],
        limit: int = 5,
    ) -> str:
        """Search Gmail; returns a numbered list of senders and subjects."""
        return search(query, limit)

    @mcp.tool(name="read", annotations={"readOnlyHint": True})
    def read_tool(
        message_id: Annotated[
            str,
            Field(
                description="Which email from the last unread/search list: its "
                "number ('2'), or words from the sender or subject. Never make "
                "up an id."
            ),
        ],
    ) -> str:
        """Read one email's text."""
        return read(message_id)

    @mcp.tool(
        name="draft", annotations={"readOnlyHint": False, "destructiveHint": False}
    )
    def draft_tool(
        to: Annotated[str, Field(description="Addresses or contact names.")],
        subject: str,
        body: Annotated[str, Field(description="The full email you wrote.")],
    ) -> str:
        """Save an email as a Gmail draft for the user to review."""
        return draft(to, subject, body)

    @mcp.tool(name="send", annotations={"readOnlyHint": False, "destructiveHint": True})
    def send_tool(
        to: Annotated[str, Field(description="Addresses or contact names.")],
        subject: str,
        body: Annotated[str, Field(description="The full email you wrote.")],
    ) -> str:
        """Send an email from the user's Gmail (they confirm first)."""
        return send(to, subject, body)

    return mcp


__all__ = [
    "GmailError",
    "authorize",
    "build_gmail_server",
    "describe_send",
    "draft",
    "gmail_available",
    "is_connected",
    "read",
    "resolve",
    "search",
    "send",
    "unread",
]
