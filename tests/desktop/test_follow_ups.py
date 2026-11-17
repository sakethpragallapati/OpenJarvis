"""Context between commands: follow-ups, corrections, contacts, code, honesty."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from openjarvis.desktop import code as code_mod
from openjarvis.desktop import server
from openjarvis.desktop.contacts import (
    Directory,
    despell,
    parse_contacts,
    phonetic,
    score_name,
)
from openjarvis.desktop.fastpath import FastIntent, match
from openjarvis.desktop.recent import Recent
from openjarvis.desktop.whatsapp import ChatRow, _search_terms, best_chat

MISSING_TRISHA = (
    "I couldn't find a WhatsApp chat called 'Trisha'. Did you mean Thrisha ECE2, "
    "Trishul Reddy or Krishna? Say which."
)


def _intent(text: str, recent: Recent) -> tuple:
    found = match(text, recent=recent)
    return (found.tool, found.args) if isinstance(found, FastIntent) else None


# ---------------------------------------------------------------------------
# What just happened
# ---------------------------------------------------------------------------


def test_recent_tracks_each_kind_of_result() -> None:
    recent = Recent()
    recent.note("open", {"target": "vs code"}, "Opened Visual Studio Code.", True)
    recent.note(
        "open",
        {"target": "minecraft folder"},
        r"Opened Minecraft Server (D:\Games\Minecraft Server).",
        True,
    )
    recent.note(
        "whatsapp",
        {"action": "open_chat", "contact": "anand"},
        "Opened your WhatsApp chat with Anand ECE-2.",
        True,
    )
    recent.note(
        "browser_search",
        {"query": "nvidia stock price", "site": "google"},
        "Searching google for nvidia stock price.",
        True,
    )
    recent.note(
        "fs_search_files",
        {"path": "C:\\Users\\me", "pattern": "**/*resume*"},
        "C:\\Users\\me\\Documents\\resume.pdf\nC:\\Users\\me\\Desktop\\resume old.docx",
        True,
    )
    assert recent.app == "Visual Studio Code"
    assert recent.path.endswith("resume.pdf") and len(recent.paths) == 2
    assert recent.contact == "Anand ECE-2"
    summary = recent.summary()
    assert "Last contact (him/her/them): Anand ECE-2" in summary
    assert "'nvidia stock price' on google" in summary
    assert "FAILED" not in summary


def test_failures_are_summarised_then_go_stale(monkeypatch) -> None:
    recent = Recent()
    recent.note("whatsapp", {"action": "open_chat", "contact": "Trisha"}, "nope", False)
    assert "Your previous step FAILED: whatsapp(" in recent.summary()
    import openjarvis.desktop.recent as recent_mod

    real = recent_mod.time.time
    monkeypatch.setattr(recent_mod.time, "time", lambda: real() + 600)
    assert recent.recent_error() is None and "FAILED" not in recent.summary()


# ---------------------------------------------------------------------------
# Follow-ups resolved without the model
# ---------------------------------------------------------------------------


@pytest.fixture
def after_failed_chat() -> Recent:
    recent = Recent()
    recent.note(
        "whatsapp", {"action": "open_chat", "contact": "Trisha"}, MISSING_TRISHA, False
    )
    return recent


@pytest.mark.parametrize(
    ("said", "contact"),
    [
        ("It's Anand.", "Anand"),
        ("No, Anand ECE-2", "Anand ECE-2"),
        ("T-H-R-I-S-H-A ECE2", "Thrisha ECE2"),
        ("Thrisha ECE2", "Thrisha ECE2"),
        ("the second one", "Trishul Reddy"),
        ("Krishna", "Krishna"),
    ],
)
def test_corrections_retry_the_failed_chat(
    after_failed_chat: Recent, said: str, contact: str
) -> None:
    assert _intent(said, after_failed_chat) == (
        "whatsapp",
        {"action": "open_chat", "contact": contact},
    )


@pytest.mark.parametrize(
    "said",
    ["okay", "never mind", "open youtube", "what time is it", "bear the next to her"],
)
def test_other_speech_is_not_taken_as_a_name(after_failed_chat: Recent, said) -> None:
    found = _intent(said, after_failed_chat)
    assert found is None or found[0] != "whatsapp"


def test_try_again_repeats_the_failed_step(after_failed_chat: Recent) -> None:
    assert _intent("try again", after_failed_chat) == (
        "whatsapp",
        {"action": "open_chat", "contact": "Trisha"},
    )


def test_pronouns_mean_the_last_contact() -> None:
    recent = Recent()
    recent.note(
        "whatsapp",
        {"action": "open_chat", "contact": "anand"},
        "Opened your WhatsApp chat with Anand ECE-2.",
        True,
    )
    send = {"action": "send", "contact": "Anand ECE-2"}
    assert _intent("tell him I'm on my way", recent) == (
        "whatsapp",
        {**send, "message": "I'm on my way"},
    )
    assert _intent("send hi to him", recent) == ("whatsapp", {**send, "message": "hi"})
    assert _intent("send a message to him saying see you soon", recent) == (
        "whatsapp",
        {**send, "message": "see you soon"},
    )
    assert _intent("Open his chat again", recent) == (
        "whatsapp",
        {"action": "open_chat", "contact": "Anand ECE-2"},
    )
    assert _intent("Did he reply?", recent) == (
        "whatsapp",
        {"action": "read", "contact": "Anand ECE-2"},
    )


@pytest.mark.parametrize(
    ("said", "which"),
    [
        ("Play the next one", 0),
        ("Jarvis, stop this current music and play the next one", 0),
        ("next video", 0),
        ("skip this video", 0),
        ("Jarvis, bear the next to her", 0),  # misheard, still clearly "next"
        ("play the second one", 2),
    ],
)
def test_next_video_continues_the_same_search(said: str, which: int) -> None:
    recent = Recent()
    recent.played("phonk", [("a", "One"), ("b", "Two"), ("c", "Three")], 0)
    assert _intent(said, recent) == ("play_youtube", {"which": which})


def test_next_is_only_a_video_when_one_is_playing() -> None:
    assert _intent("next tab", Recent()) == (
        "keyboard",
        {"action": "hotkey", "text": "ctrl+tab"},
    )
    recent = Recent()
    recent.played("phonk", [("a", "One")], 0)
    assert _intent("next tab", recent)[0] == "keyboard"


def test_it_and_that_mean_the_last_app_search_or_program() -> None:
    recent = Recent()
    recent.note("open", {"target": "vs code"}, "Opened Visual Studio Code.", True)
    recent.note(
        "browser_search",
        {"query": "nvidia stock price", "site": "google"},
        "Searching google for nvidia stock price.",
        True,
    )
    recent.code_file = r"C:\Users\me\Desktop\Jarvis Code\hello.py"
    assert _intent("close it", recent) == (
        "window",
        {"action": "close", "name": "Visual Studio Code"},
    )
    assert _intent("search that on youtube", recent) == (
        "browser_search",
        {"query": "nvidia stock price", "site": "youtube"},
    )
    for said in (
        "run it",
        "Jarvis, save the program and run the execution please and show me the output",
        "show me the output",
    ):
        assert _intent(said, recent) == ("code", {"action": "run"}), said
    # Changing the program is a job for the model, not a plain re-run.
    assert _intent("change it to print hello twice and run it", recent) is None


@pytest.mark.parametrize(
    ("said", "contact"),
    [
        ("Open WhatsApp and open Trisha chat", "Trisha"),
        ("Open WhatsApp and open Anand the chat.", "Anand"),
        ("Open Arnold's chat.", "Arnold"),
    ],
)
def test_whatsapp_requests_keep_only_the_name(said: str, contact: str) -> None:
    assert _intent(said, Recent()) == (
        "whatsapp",
        {"action": "open_chat", "contact": contact},
    )


def test_contacts_list_by_voice() -> None:
    assert _intent("open my contacts list", Recent()) == (
        "contacts",
        {"action": "open"},
    )
    assert _intent("Add Trisha to my contacts as Thrisha ECE2", Recent()) == (
        "contacts",
        {"action": "add", "spoken": "Trisha", "name": "Thrisha ECE2"},
    )
    assert _intent("When I say mom, I mean Amma", Recent()) == (
        "contacts",
        {"action": "add", "spoken": "mom", "name": "Amma"},
    )


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def test_spelled_and_sound_alike_names() -> None:
    assert despell("T-H-R-I-S-H-A ECE2") == "Thrisha ECE2"
    assert despell("it's A N A N D") == "it's Anand"
    assert despell("Anand ECE-2") == "Anand ECE-2"
    assert phonetic("Trisha") == phonetic("Thrisha")
    assert phonetic("Saketh") == phonetic("Saket")
    assert score_name("trisha", "Thrisha ECE2") >= 0.8
    assert score_name("anand", "Anand ECE-2") >= 0.9
    assert score_name("arnold", "Anand ECE-2") < 0.6


def test_chat_lookup_tolerates_mishearing() -> None:
    rows = [
        ChatRow("Thrisha ECE2", 0, "", (1, 1)),
        ChatRow("Anand ECE-2", 0, "", (2, 2)),
        ChatRow("Daddy", 0, "", (3, 3)),
    ]
    assert best_chat(rows, "Trisha").title == "Thrisha ECE2"
    assert best_chat(rows, "T-H-R-I-S-H-A").title == "Thrisha ECE2"
    assert best_chat(rows, "anand").title == "Anand ECE-2"
    assert best_chat(rows, "Arnold") is None
    # WhatsApp's search matches substrings, so shorter pieces find "Thrisha".
    assert _search_terms("Trisha") == ["Trisha", "risha", "isha"]


def test_contacts_file_and_learned_names(tmp_path: Path) -> None:
    contacts = tmp_path / "contacts.txt"
    directory = Directory(contacts, tmp_path / "cache.json")
    created = contacts.read_text(encoding="utf-8")
    assert created.startswith("# Jarvis contacts:") and "[nicknames]" in created
    contacts.write_text(
        "# mine\nTrisha = Thrisha ECE2\nMom = Amma\nVishnu Vardhan AIDS\n",
        encoding="utf-8",
    )
    assert parse_contacts(contacts.read_text(encoding="utf-8"))[0] == {
        "trisha": "Thrisha ECE2",
        "mom": "Amma",
    }
    assert directory.alias("trisha") == "Thrisha ECE2"
    assert directory.alias("T-R-I-S-H-A") == "Thrisha ECE2"
    assert directory.resolve("vishnu") == "Vishnu Vardhan AIDS"
    directory.saw(["Anand ECE-2", "Anand Kumar"])
    assert directory.resolve("Anand") is None  # two Anands: ask, don't guess
    directory.learn("Arnold", "Anand ECE-2")
    reopened = Directory(contacts, tmp_path / "cache.json")
    assert reopened.alias("arnold") == "Anand ECE-2"
    assert reopened.forget("Arnold") and reopened.alias("arnold") is None
    assert json.loads((tmp_path / "cache.json").read_text())["seen"]


def test_contacts_tool(tmp_path: Path, monkeypatch) -> None:
    import openjarvis.desktop.contacts as contacts_mod

    path = tmp_path / "contacts.txt"
    path.write_text("[contacts]\nAmma\nSai Teja (Tammudu)\n", encoding="utf-8")
    directory = Directory(path, tmp_path / "cache.json")
    monkeypatch.setattr(contacts_mod, "_DIRECTORY", directory)
    assert "Saved" in server.contacts("add", "Mom", "amma")
    assert "Saved" in server.contacts("add", "Bro", "Sai Teja (Tammudu)")
    listing = server.contacts("list")
    assert "I know 2 names" in listing and "mom = Amma" in listing
    assert directory.alias("Mom") == "Amma"
    # Nicknames only point at real contacts, and never at themselves.
    with pytest.raises(server.DesktopError, match="no contact called 'on'"):
        server.contacts("add", "onn", "on")
    with pytest.raises(server.DesktopError, match="has to differ"):
        server.contacts("add", "Amma", "Amma")


# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------


def test_code_write_then_run_returns_the_real_output(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(server.CONTEXT, "code_dir", str(tmp_path))
    monkeypatch.setattr(server.CONTEXT, "recent", Recent())
    opened: List[Path] = []
    monkeypatch.setattr(
        code_mod, "open_in_editor", lambda p: opened.append(p) or "VS Code"
    )
    wrote = server.code("write", "hello", 'print("Hello World")', "python")
    assert "hello.py" in wrote and opened == [tmp_path / "hello.py"]
    assert server.CONTEXT.recent.code_file == str(tmp_path / "hello.py")
    ran = server.code("run")
    assert "Hello World" in ran and ran.startswith("Ran hello.py.")
    # Its own folder runs without asking; elsewhere asks first.
    assert server.confirmation_question("code", {"action": "run"}) is None
    other = tmp_path.parent / "elsewhere.py"
    assert "Run the program" in server.confirmation_question(
        "code", {"action": "run", "filename": str(other)}
    )


def test_failing_program_reports_the_error(tmp_path: Path) -> None:
    path = code_mod.write("bad", "raise SystemExit('boom')", "python", folder=tmp_path)
    result = code_mod.run(path)
    assert "failed with exit code 1" in result and "boom" in result
    assert code_mod.command_for(tmp_path / "x.unknown") is None
    assert code_mod.command_for(path)[-1] == str(path)
    assert code_mod.resolve("BAD", tmp_path) == path


# ---------------------------------------------------------------------------
# The agent loop: guard, honesty, plan, context
# ---------------------------------------------------------------------------

pytest.importorskip("langgraph")

from openjarvis.desktop.graph import (  # noqa: E402
    DesktopGraph,
    honest_reply,
    unrequested,
    with_defaults,
)
from openjarvis.desktop.runtime import ToolOutcome  # noqa: E402


def _call(name: str, **args: Any) -> Dict[str, Any]:
    return {"id": f"c_{name}", "name": name, "arguments": json.dumps(args)}


class Runtime:
    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.schemas = [{"type": "function", "function": {"name": "open"}}]
        self.names = ["open", "whatsapp", "system", "web_search"]

    def annotations(self, name: str) -> Any:
        return None

    def call(self, name: str, args: Dict[str, Any] | None = None) -> ToolOutcome:
        self.calls.append((name, args))
        if name == "whatsapp":
            return ToolOutcome(name, MISSING_TRISHA, False)
        return ToolOutcome(name, f"Did {name}.", True)


class Model:
    def __init__(self, replies: List[Dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.seen: List[Any] = []

    def __call__(self, messages, tools=None) -> Dict[str, Any]:
        self.seen.append((messages, tools))
        return self.replies.pop(0) if self.replies else {"content": "Done."}


def test_unrequested_disruptive_actions_are_refused() -> None:
    assert unrequested("system", {"action": "lock"}, "Make a call to me") is not None
    assert unrequested("system", {"action": "lock"}, "lock my laptop") is None
    assert unrequested("system", {"action": "volume_up"}, "anything") is None
    assert unrequested("window", {"action": "close"}, "open notepad") is not None


def test_honest_reply_after_a_decline_or_failure() -> None:
    declined = [
        {
            "name": "whatsapp",
            "ok": False,
            "text": "The user did not allow this, so it was not done.",
        }
    ]
    assert honest_reply("I've already sent you the message.", declined) == (
        "Okay, I didn't do that."
    )
    failed = [{"name": "code", "ok": False, "text": "It failed with exit code 1"}]
    assert honest_reply("Saved and executed successfully.", failed).startswith(
        "I couldn't finish that."
    )
    assert honest_reply("I couldn't run it: exit code 1.", failed) == (
        "I couldn't run it: exit code 1."
    )
    assert honest_reply("Done.", [{"ok": True, "text": "x"}]) == "Done."


def test_sloppy_action_names_are_repaired() -> None:
    assert (
        with_defaults("keyboard", {"action": "hotkey>", "text": "ctrl+n"}, "")["action"]
        == "hotkey"
    )
    assert (
        with_defaults("whatsapp", {"action": "Open-Chat"}, "")["action"] == "open_chat"
    )


def test_lock_instead_of_a_call_is_blocked_and_admitted() -> None:
    runtime = Runtime()
    model = Model(
        [
            {"content": "", "tool_calls": [_call("system", action="lock")]},
            {"content": "Done, I've called them."},
        ]
    )
    graph = DesktopGraph(generate=model, runtime=runtime)
    state = graph.invoke("Make a call to Anand")
    assert runtime.calls == []  # never locked the screen
    # Not "Done, I've called them", and no internal "Not done: ..." note.
    assert state["reply"] == "I couldn't do that."


def test_context_and_failures_reach_the_model() -> None:
    recent = Recent()
    runtime = Runtime()
    model = Model(
        [
            {
                "content": "",
                "tool_calls": [_call("whatsapp", action="open_chat", contact="Trisha")],
            },
            {"content": "I couldn't find Trisha. Did you mean Thrisha ECE2?"},
            {"content": "Sure."},
        ]
    )
    graph = DesktopGraph(generate=model, runtime=runtime, recent=recent)
    graph.invoke("open Trisha chat")
    assert recent.recent_error()["tool"] == "whatsapp"
    graph.invoke("hmm what now")
    note = model.seen[-1][0][-1].content
    assert "Your previous step FAILED: whatsapp(" in note and "retry" in note


def test_multi_part_requests_get_a_plan_first() -> None:
    runtime = Runtime()
    model = Model(
        [
            {"content": "1. open google\n2. web_search nvidia\n3. web_search google"},
            {"content": "", "tool_calls": [_call("open", target="google")]},
            {"content": "Nvidia is up, Google is down."},
        ]
    )
    graph = DesktopGraph(generate=model, runtime=runtime, plan=True)
    graph.invoke("open google and compare nvidia with google stock price")
    plan_messages, plan_tools = model.seen[0]
    assert plan_tools is None and "plan tool steps" in plan_messages[0].content
    first_step = model.seen[1][0][-1].content
    assert "Plan (do every step" in first_step and "3. web_search google" in first_step
    # A one-step request doesn't pay for a plan.
    model.replies = [{"content": "", "tool_calls": [_call("open", target="x")]}]
    before = len(model.seen)
    graph.invoke("Jarvis, open notepad")
    assert len(model.seen) == before + 1


# ---------------------------------------------------------------------------
# Always listening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "in_conversation", "expecting", "verdict"),
    [
        ("Now compare it with Google stock price please", False, False, "command"),
        ("Work on the saving and run the output please", False, False, "command"),
        ("Can you see my screen?", False, False, "command"),
        ("What's on my screen right now", False, False, "question"),
        ("It's Anand.", True, True, "command"),
        ("Anand", True, True, "command"),
        ("yes", True, True, "command"),
        ("okay", True, False, "ignore"),
        ("Draft a poem for him and send it", True, False, "command"),
        ("We will see you in the next one", False, False, "ignore"),
        ("What are you doing tonight?", False, False, "ignore"),
    ],
)
def test_follow_ups_are_heard_in_a_conversation(
    text: str, in_conversation: bool, expecting: bool, verdict: str
) -> None:
    from openjarvis.cli import _assistant

    assert (
        _assistant.classify_utterance(
            text, in_conversation=in_conversation, expecting=expecting
        )
        == verdict
    )


def test_a_failed_or_questioning_reply_keeps_listening_longer() -> None:
    from openjarvis.cli import _assistant

    listener = _assistant.AlwaysListener()
    listener.replied("Opened Notepad.")
    assert not listener.expecting
    listener.replied("I couldn't find a WhatsApp chat called 'Trisha'.", ok=False)
    assert listener.expecting and listener.in_conversation()
    listener.replied("Which one do you mean?")
    assert listener.expecting


# ---------------------------------------------------------------------------
# Screen regions, YouTube "next", long text
# ---------------------------------------------------------------------------


def test_screen_questions_can_target_part_of_the_screen() -> None:
    from openjarvis.desktop.screen import region_for

    assert region_for("can you see the left side of the chat list") == "left"
    assert region_for("what's in the bottom right corner") == "bottom right"
    assert region_for("what's on my screen") is None


def test_play_youtube_next_reuses_the_results(monkeypatch) -> None:
    from openjarvis.desktop import web

    searches: List[str] = []
    opened: List[str] = []
    results = [("v1", "First"), ("v2", "Second"), ("v3", "Third")]
    monkeypatch.setattr(
        web, "youtube_results", lambda q, n: searches.append(q) or results
    )
    monkeypatch.setattr(server.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(server, "_mark_opening", lambda: None)
    monkeypatch.setattr(server.CONTEXT, "recent", Recent())
    assert server.play_youtube("phonk") == "Playing First on YouTube."
    assert server.play_youtube(which=0) == "Playing Second on YouTube."
    assert server.play_youtube("", which=0) == "Playing Third on YouTube."
    with pytest.raises(server.DesktopError, match="last result"):
        server.play_youtube(which=0)
    assert searches == ["phonk"] and opened[-1].endswith("v3")


@pytest.mark.skipif(sys.platform != "win32", reason="SendInput is Windows-only")
def test_long_text_is_pasted_not_typed(monkeypatch) -> None:
    from openjarvis.desktop import winapi

    pasted: List[str] = []
    sent: List[Any] = []
    monkeypatch.setattr(winapi, "paste_text", lambda t: pasted.append(t) or True)
    monkeypatch.setattr(winapi, "_send", sent.append)
    winapi.type_text("x" * 400)
    assert pasted and not sent
    winapi.type_text("short")
    assert sent


# ---------------------------------------------------------------------------
# Facts must come from tools; calls are real and confirmed
# ---------------------------------------------------------------------------


def test_numbers_must_come_from_a_tool_result() -> None:
    from openjarvis.desktop.graph import ungrounded_numbers

    sources = ["GOOGL stock: $281.10, -0.4% today."]
    assert ungrounded_numbers("Google is at $281.10.", sources) == []
    assert ungrounded_numbers("Nvidia is around $950 and Google $281.10.", sources) == [
        "$950"
    ]
    assert ungrounded_numbers("Step 1 done, 2 left.", []) == []  # counting is fine


def test_a_made_up_price_is_sent_back_once() -> None:
    class FactsRuntime(Runtime):
        def call(self, name, args=None):
            self.calls.append((name, args))
            return ToolOutcome(name, "NVDA stock: $186.40 today.", True)

    runtime = FactsRuntime()
    model = Model(
        [
            {"content": "Nvidia trades at about $950."},
            {
                "content": "",
                "tool_calls": [_call("web_search", query="nvidia stock price")],
            },
            {"content": "Nvidia is at $186.40 today."},
        ]
    )
    graph = DesktopGraph(generate=model, runtime=runtime)
    state = graph.invoke("what's the nvidia stock price")
    assert runtime.calls == [("web_search", {"query": "nvidia stock price"})]
    assert state["reply"] == "Nvidia is at $186.40 today."
    nudge = model.seen[1][0][-1].content
    assert "$950 did not come from any tool result" in nudge


@pytest.mark.parametrize(
    ("said", "args"),
    [
        ("Call Anand", {"action": "call", "contact": "Anand"}),
        (
            "Make a video call to Daddy on WhatsApp",
            {"action": "video_call", "contact": "Daddy"},
        ),
        (
            "start a call with Thrisha ECE2",
            {"action": "call", "contact": "Thrisha ECE2"},
        ),
    ],
)
def test_calls_go_to_whatsapp(said: str, args: dict) -> None:
    assert _intent(said, Recent()) == ("whatsapp", args)


def test_call_pronouns_and_idioms() -> None:
    recent = Recent()
    assert _intent("call it a day", recent) is None
    assert _intent("Make a call to me", recent) is None
    recent.note(
        "whatsapp",
        {"action": "open_chat", "contact": "anand"},
        "Opened your WhatsApp chat with Anand ECE-2.",
        True,
    )
    assert _intent("make a call to him", recent) == (
        "whatsapp",
        {"action": "call", "contact": "Anand ECE-2"},
    )
    assert _intent("video call him", recent)[1]["action"] == "video_call"
    assert (
        server.confirmation_question(
            "whatsapp", {"action": "call", "contact": "Anand ECE-2"}
        )
        == "Start a WhatsApp voice call with Anand ECE-2?"
    )


# ---------------------------------------------------------------------------
# Importing phone contacts
# ---------------------------------------------------------------------------

GOOGLE_CSV = (
    "First Name,Middle Name,Last Name,Nickname,Labels,Phone 1 - Label,Phone 1 - Value\n"
    "Thrisha ECE-2,,,,* myContacts,Mobile,+91 00000 00001\n"
    "Krishna,,IT-1 Jr,,* myContacts,Mobile,+91 00000 00002\n"
    "Krishna Kaushik,,,,* myContacts,Mobile,+91 00000 00003\n"
    "Krishna,,Sameer,Sam,* myContacts,Mobile,+91 00000 00004\n"
    "Amma,,,,* myContacts,Mobile,+91 00000 00005\n"
    "Amma,(office phone number),,,* myContacts,Mobile,+91 00000 00006\n"
    "Baba,,,,* myContacts,Mobile,+91 00000 00007\n"
    "Baba,,,,* myContacts,Mobile,+91 00000 00008\n"
    ",,,,* myContacts,Mobile,+91 00000 00009\n"
)


def test_import_google_contacts_keeps_names_only(tmp_path: Path) -> None:
    from openjarvis.desktop.contacts import import_file, read_contact_names

    source = tmp_path / "contacts.csv"
    source.write_text(GOOGLE_CSV, encoding="utf-8")
    names, nicknames = read_contact_names(source)
    assert names[:3] == ["Thrisha ECE-2", "Krishna IT-1 Jr", "Krishna Kaushik"]
    assert nicknames == {"Sam": "Krishna Sameer"}
    book = Directory(tmp_path / "contacts.txt", tmp_path / "cache.json")
    (tmp_path / "contacts.txt").write_text(
        "Mom = Amma\nRavi Teja\n", encoding="utf-8"
    )  # the user's own lines, written before any import
    count, added = import_file(source, book)
    text = (tmp_path / "contacts.txt").read_text(encoding="utf-8")
    assert "+91" not in text  # phone numbers are not copied
    assert count == 7 and added == 1  # duplicates merged
    sections = text.split("\n\n")
    assert "[nicknames]\nMom = Amma\nSam = Krishna Sameer" in sections
    assert "[other names]\nRavi Teja" in sections
    contacts = text.split("[contacts]\n", 1)[1].splitlines()
    assert contacts[0].startswith("# 7 names from contacts.csv, imported ")
    assert contacts[1:] == [
        "Amma",
        "Amma (office phone number)",
        "Baba",
        "Krishna IT-1 Jr",
        "Krishna Kaushik",
        "Krishna Sameer",
        "Thrisha ECE-2",
    ]
    assert book.alias("sam") == "Krishna Sameer" and book.alias("mom") == "Amma"
    # Importing again replaces [contacts] and keeps everything else.
    import_file(source, book)
    assert (tmp_path / "contacts.txt").read_text(encoding="utf-8") == text


def test_old_layout_converts_and_nicknames_go_in_their_section(tmp_path: Path) -> None:
    path = tmp_path / "contacts.txt"
    path.write_text(
        "# Jarvis contacts.\nTrisha = Thrisha ECE-2\n\n"
        "# --- imported from contacts.csv (2026-11-15); re-importing replaces\n"
        "Anand ECE-2\nDaddy\n# --- end of import ---\n",
        encoding="utf-8",
    )
    book = Directory(path, tmp_path / "cache.json")
    tidy = book.tidy()
    assert tidy.nicknames == [("Trisha", "Thrisha ECE-2")]
    assert tidy.contacts == ["Anand ECE-2", "Daddy"]
    book.add_nickname("Mom", "Amma")
    book.add_nickname("trisha", "Thrisha ECE-2")  # replaces, no duplicate
    text = path.read_text(encoding="utf-8")
    assert "[nicknames]\nMom    = Amma\ntrisha = Thrisha ECE-2\n" in text
    nicknames = text.split("[nicknames]\n", 1)[1].split("\n\n", 1)[0]
    assert nicknames.count("Thrisha ECE-2") == 1 and "# --- imported" not in text
    assert "# 2 names. Importing your phone contacts again replaces" in text


def test_matching_names_need_no_nickname_or_memory(tmp_path: Path, monkeypatch) -> None:
    import openjarvis.desktop.contacts as contacts_mod

    path = tmp_path / "contacts.txt"
    path.write_text("[contacts]\nThrisha ECE-2\nAnand ECE-2\n", encoding="utf-8")
    book = Directory(path, tmp_path / "cache.json")
    monkeypatch.setattr(contacts_mod, "_DIRECTORY", book)
    assert "No need" in server.contacts("add", "Trisha", "Thrisha ECE-2")
    book.learn("Trisha", "Thrisha ECE-2")  # sounds alike: nothing to remember
    book.learn("Arnold", "Anand ECE-2")  # misheard: worth remembering
    assert book.learned == {"arnold": "Anand ECE-2"}


def test_imported_names_resolve_or_ask(tmp_path: Path) -> None:
    from openjarvis.desktop.contacts import import_file

    source = tmp_path / "contacts.csv"
    source.write_text(GOOGLE_CSV, encoding="utf-8")
    book = Directory(tmp_path / "contacts.txt", tmp_path / "cache.json")
    import_file(source, book)
    assert book.resolve("Trisha") == "Thrisha ECE-2"
    assert book.resolve("Amma") == "Amma"  # exact beats "Amma (office...)"
    assert book.ambiguous("Amma") == []
    assert book.ambiguous("Krishna") == [
        "Krishna IT-1 Jr",
        "Krishna Kaushik",
        "Krishna Sameer",
    ]


def test_vcard_names(tmp_path: Path) -> None:
    from openjarvis.desktop.contacts import read_contact_names

    card = tmp_path / "contacts.vcf"
    card.write_text(
        "BEGIN:VCARD\nVERSION:3.0\nFN:Anand ECE-2\nTEL:+91 00000 00001\nEND:VCARD\n"
        "BEGIN:VCARD\nVERSION:3.0\nFN;CHARSET=UTF-8:Daddy\nEND:VCARD\n",
        encoding="utf-8",
    )
    assert read_contact_names(card) == (["Anand ECE-2", "Daddy"], {})


def test_which_krishna_then_the_second_one() -> None:
    recent = Recent()
    recent.note(
        "whatsapp",
        {"action": "open_chat", "contact": "Krishna"},
        "There's more than one 'Krishna' in your contacts. Did you mean "
        "Krishna IT-1 Jr, Krishna Kaushik or Krishna Sameer? Say which.",
        False,
    )
    assert _intent("the second one", recent) == (
        "whatsapp",
        {"action": "open_chat", "contact": "Krishna Kaushik"},
    )
    assert _intent("Krishna Sameer", recent) == (
        "whatsapp",
        {"action": "open_chat", "contact": "Krishna Sameer"},
    )


def test_contacts_cli(tmp_path: Path, monkeypatch) -> None:
    from click.testing import CliRunner

    import openjarvis.desktop.contacts as contacts_mod
    from openjarvis.cli.desktop_cmd import desktop

    book = Directory(tmp_path / "contacts.txt", tmp_path / "cache.json")
    monkeypatch.setattr(contacts_mod, "_DIRECTORY", book)
    source = tmp_path / "contacts.csv"
    source.write_text(GOOGLE_CSV, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(desktop, ["contacts", "import", str(source)])
    assert result.exit_code == 0 and "Imported 7 names and 1 nicknames" in result.output
    found = runner.invoke(desktop, ["contacts", "find", "Krishna"])
    assert "could be: Krishna IT-1 Jr" in found.output
    assert (
        "'Trisha' -> Thrisha ECE-2"
        in runner.invoke(desktop, ["contacts", "find", "Trisha"]).output
    )
