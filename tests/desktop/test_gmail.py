"""Gmail MCP server against a fake Gmail API."""

from __future__ import annotations

import base64
import email
import email.policy
from typing import Any, Dict, List

import pytest

from openjarvis.desktop import gmail


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


MESSAGES: Dict[str, Dict[str, Any]] = {
    "m1": {
        "id": "m1",
        "snippet": "Interviews on 16th &amp; 17th",
        "payload": {
            "headers": [
                {"name": "From", "value": "Placement Cell <tpo@cbit.ac.in>"},
                {"name": "To", "value": "me@gmail.com"},
                {"name": "Subject", "value": "Deloitte interviews"},
                {"name": "Date", "value": "Fri, 13 Nov 2026 19:45:00 +0530"},
            ],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Report at 9 am.\n")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>Report</p>")}},
            ],
        },
    },
    "m2": {
        "id": "m2",
        "snippet": "See you",
        "payload": {
            "headers": [
                {"name": "From", "value": "Ravi Kumar <ravi.k@example.com>"},
                {"name": "To", "value": "me@gmail.com"},
                {"name": "Subject", "value": "Sunday?"},
            ],
            "mimeType": "text/html",
            "body": {"data": _b64("<div>Free on <b>Sunday</b>?<br>Ravi</div>")},
        },
    },
}


class _Call:
    def __init__(self, result: Any) -> None:
        self.result = result

    def execute(self) -> Any:
        return self.result


class FakeGmail:
    def __init__(self) -> None:
        self.queries: List[str] = []
        self.sent: List[str] = []
        self.saved_drafts: List[str] = []

    def users(self) -> "FakeGmail":
        return self

    def messages(self) -> "FakeGmail":
        return self

    def drafts(self):  # noqa: D401 - mimics the API's attribute
        outer = self

        class Drafts:
            def create(self, userId: str, body: Dict[str, Any]) -> _Call:
                outer.saved_drafts.append(body["message"]["raw"])
                return _Call({"id": "d1"})

        return Drafts()

    def list(self, userId: str, q: str, maxResults: int) -> _Call:
        self.queries.append(q)
        ids = ["m1"] if "unread" in q else ["m2"] if "ravi" in q else ["m1", "m2"]
        return _Call({"messages": [{"id": i} for i in ids][:maxResults]})

    def get(self, userId: str, id: str, **kwargs: Any) -> _Call:
        return _Call(MESSAGES[id])

    def send(self, userId: str, body: Dict[str, Any]) -> _Call:
        self.sent.append(body["raw"])
        return _Call({"id": "s1"})

    def getProfile(self, userId: str) -> _Call:
        return _Call({"emailAddress": "me@gmail.com"})


@pytest.fixture
def svc(monkeypatch) -> FakeGmail:
    fake = FakeGmail()
    monkeypatch.setattr(gmail, "service", lambda: fake)
    monkeypatch.setattr(gmail, "_RESOLVED", {})
    return fake


def _decoded(raw: str) -> email.message.Message:
    return email.message_from_bytes(
        base64.urlsafe_b64decode(raw), policy=email.policy.default
    )


def test_unread_and_search(svc: FakeGmail) -> None:
    assert gmail.unread() == "1 unread emails:\n1. Placement Cell: Deloitte interviews"
    found = gmail.search("from:tpo")
    assert found.startswith("1. Placement Cell: Deloitte interviews")
    assert "Interviews on 16th & 17th" in found and "m1" not in found


def test_read_prefers_plain_text_and_strips_html(svc: FakeGmail) -> None:
    gmail.search("anything")  # lists m1, m2
    assert "Report at 9 am." in gmail.read("1")
    text = gmail.read("the second one")
    assert "Free on Sunday ?" in text.replace("  ", " ") or "Free on" in text
    assert "<b>" not in text


def test_emails_are_picked_from_the_last_list_never_by_made_up_ids(
    svc: FakeGmail,
) -> None:
    gmail.search("anything")
    assert "Free on" in gmail.read("Ravi")  # sender words
    assert "Report at 9 am." in gmail.read("deloitte")  # subject words
    assert "Report at 9 am." in gmail.read("first")
    with pytest.raises(ValueError, match=r"From the last list: 1\. Placement Cell"):
        gmail.read("the email from Priya")  # the model's invented search
    with pytest.raises(ValueError, match="doesn't exist"):

        class Missing(FakeGmail):
            def get(self, userId: str, id: str, **kwargs: Any) -> _Call:
                raise RuntimeError(
                    "<HttpError 404 ... Requested entity was not found.>"
                )

        gmail.read("18023456789aaa", svc=Missing())


def test_names_resolve_from_mail_headers(svc: FakeGmail) -> None:
    assert gmail.resolve("Ravi, x@y.com") == (["ravi.k@example.com", "x@y.com"], [])
    assert gmail.resolve("Nobody Known") == ([], ["Nobody Known"])


def test_draft_and_send_build_real_messages(svc: FakeGmail) -> None:
    assert "ravi.k@example.com" in gmail.draft("ravi", "Sunday", "Free?")
    message = _decoded(svc.saved_drafts[0])
    assert message["To"] == "ravi.k@example.com" and message["Subject"] == "Sunday"
    assert gmail.send("x@y.com", "Hi", "Hello there") == "Sent your email to x@y.com."
    assert _decoded(svc.sent[0]).get_content().strip() == "Hello there"


def test_unknown_recipient_never_sends(svc: FakeGmail) -> None:
    with pytest.raises(gmail.GmailError, match="don't know the email address"):
        gmail.send("Nobody Known", "Hi", "Hello")
    assert svc.sent == []
    assert gmail.describe_send({"to": "Nobody Known", "subject": "Hi"}) is None


def test_confirmation_reads_back_the_real_address(svc: FakeGmail) -> None:
    from openjarvis.desktop import server

    question = server.confirmation_question(
        "gmail_send", {"to": "Ravi", "subject": "Sunday?", "body": "x"}
    )
    assert question == "Send the email 'Sunday?' to ravi.k@example.com?"
    assert server.confirmation_question("gmail_draft", {"to": "Ravi"}) is None


def test_not_connected_message(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gmail, "token_path", lambda: tmp_path / "token.json")
    monkeypatch.setattr(gmail, "_SERVICE", {})
    with pytest.raises(gmail.GmailError, match="gmail-auth"):
        gmail.service()


def test_gmail_tools_are_mounted_and_replace_the_browser_draft() -> None:
    pytest.importorskip("fastmcp")
    from openjarvis.desktop import server
    from openjarvis.desktop.runtime import ToolRuntime

    runtime = ToolRuntime(server.build_server(gmail=True, files=False))
    try:
        names = set(runtime.names)
        assert {
            "gmail_unread",
            "gmail_search",
            "gmail_read",
            "gmail_draft",
            "gmail_send",
        } <= names
        assert "email" not in names
    finally:
        runtime.close()


def test_unread_email_fast_path_only_with_gmail() -> None:
    from openjarvis.desktop.fastpath import match

    intent = match("Any new emails?", tools={"gmail_unread"})
    assert intent is not None and intent.tool == "gmail_unread"
    assert match("Any new emails?", tools=set()) is None
