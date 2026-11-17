"""Names that are safe to match, speech-friendly replies, YouTube Music,
web-search limits, argument repair, name hints, and the voice tools."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from openjarvis.desktop import server
from openjarvis.desktop.contacts import Directory, repair_names, spoken_name
from openjarvis.desktop.fastpath import FastIntent, match
from openjarvis.desktop.recent import Recent
from openjarvis.desktop.whatsapp import ChatRow, best_chat
from openjarvis.speech.speakable import split_long, to_speech


def _intent(text: str, recent: Recent | None = None) -> tuple | None:
    found = match(text, recent=recent or Recent())
    return (found.tool, found.args) if isinstance(found, FastIntent) else None


# ---------------------------------------------------------------------------
# Never open the wrong chat
# ---------------------------------------------------------------------------

ROWS = [
    ChatRow("Codefest", 0, "", (1, 1)),
    ChatRow("Anand ECE-2", 0, "", (2, 2)),
    ChatRow("Krishna Kaushik", 0, "", (3, 3)),
    ChatRow("On", 0, "", (4, 4)),
]


def test_short_or_mid_word_names_never_match_another_chat() -> None:
    # "on" is inside "Codefest"; it once opened that group.
    assert best_chat(ROWS[:3], "on") is None
    assert best_chat(ROWS, "on").title == "On"  # an exact chat name is fine
    assert best_chat(ROWS, "sagon") is None
    assert best_chat(ROWS, "Kaushik").title == "Krishna Kaushik"  # word start


def test_corrections_are_learned_but_common_words_are_not(
    tmp_path: Path, monkeypatch
) -> None:
    import openjarvis.desktop.contacts as contacts_mod

    path = tmp_path / "contacts.txt"
    path.write_text("[contacts]\nAnand ECE-2\n", encoding="utf-8")
    book = Directory(path, tmp_path / "cache.json")
    monkeypatch.setattr(contacts_mod, "_DIRECTORY", book)
    missing = "I couldn't find a WhatsApp chat called {!r}."
    opened = "Opened your WhatsApp chat with Anand ECE-2."
    recent = Recent()
    recent.note("whatsapp", {"action": "open_chat", "contact": "Arnold"},
                missing.format("Arnold"), False)  # fmt: skip
    recent.note("whatsapp", {"action": "open_chat", "contact": "Anand"}, opened, True)
    assert book.learned == {"arnold": "Anand ECE-2"}
    recent.note("whatsapp", {"action": "open_chat", "contact": "on"},
                missing.format("on"), False)  # fmt: skip
    recent.note("whatsapp", {"action": "open_chat", "contact": "Anand"}, opened, True)
    assert "on" not in book.learned


def test_the_model_cannot_invent_nicknames() -> None:
    from openjarvis.desktop.graph import unrequested

    add = {"action": "add", "spoken": "on", "name": "on"}
    assert unrequested("contacts", add, "Please open on and in WhatsApp")
    assert unrequested("contacts", add, "add on to my contacts as Anand ECE-2") is None


# ---------------------------------------------------------------------------
# Replies: formatted on screen, clean out loud
# ---------------------------------------------------------------------------

REPORT = """Here's the comparison:

**Prices today:**
- NVIDIA (NVDA): $161.01 - Market cap: $4.01T, P/E ratio: 20.59
- Google/Alphabet: ₹29,200 vs Rs. 1,500 (~3% YoY)
1. Cloud revenue up 103% & e.g. Gemini 🙂
See https://example.com/x"""


def test_speech_reads_words_not_symbols() -> None:
    spoken = to_speech(REPORT)
    for noise in ("**", "- ", "$", "₹", "P/E", "%", "&", "https", "(", "🙂", "~"):
        assert noise not in spoken, noise
    for words in (
        "161.01 dollars",
        "4.01 trillion dollars",
        "P E ratio",
        "29,200 rupees versus 1,500 rupees",
        "about 3 percent year over year",
        "103 percent and for example Gemini",
        "a link",
    ):
        assert words in spoken, words
    # An intro ending in ':' keeps leading into the list.
    assert spoken.startswith("Here's the comparison: Prices today: NVIDIA, NVDA,")


def test_long_replies_stop_politely_and_long_sentences_split() -> None:
    long_text = " ".join(f"Point number {i} is worth saying." for i in range(60))
    spoken = to_speech(long_text, max_chars=200)
    assert spoken.endswith("The rest is on the screen.") and len(spoken) < 260
    sentence = ", ".join(f"item {i} with a few words" for i in range(30))
    pieces = split_long(sentence, 100)
    assert all(len(p) <= 100 for p in pieces) and " ".join(pieces) == sentence


def test_speech_chunks_never_exceed_the_voice_limit() -> None:
    from openjarvis.cli._voice_chat import speakable_text, split_for_speech

    run_on = ", ".join(["the phone has a large battery and a bright screen"] * 20)
    chunks = split_for_speech(speakable_text(run_on))
    assert chunks and max(len(c) for c in chunks) <= 200


def test_replies_keep_their_formatting_for_the_screen() -> None:
    from openjarvis.desktop.graph import clean_reply

    text = "<think>plan</think>Summary.\n\n- **One**\n- Two\n\n\n\nEnd."
    assert clean_reply(text) == "Summary.\n\n- **One**\n- Two\n\nEnd."


def test_a_too_long_sentence_is_said_in_halves(monkeypatch) -> None:
    import numpy as np

    pytest.importorskip("torch")
    from openjarvis.speech import chatterbox_tts
    from openjarvis.speech._chatterbox_fast import SentenceTooLong

    said: List[str] = []

    class Model:
        sr = 1000

        def generate(self, text: str):
            import torch

            if len(text) > 120:
                raise SentenceTooLong("too long")
            said.append(text)
            return torch.ones(1, 10)

    backend = chatterbox_tts.ChatterboxTTSBackend(device="cpu")
    backend._model = Model()
    backend._voice = "default"
    monkeypatch.setattr(backend, "_use_voice", lambda voice_id: "default")
    text = ", ".join(["a phone with a big battery"] * 8)  # ~220 chars
    result = backend.synthesize(text, voice_id="default")
    assert len(said) >= 2 and all(len(s) <= 120 for s in said)
    assert result.duration_seconds > 0
    assert isinstance(np.zeros(1), np.ndarray)


# ---------------------------------------------------------------------------
# YouTube Music and media
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        (
            "Jarvis, open YouTube music and play stay alive music",
            ("play_youtube", {"query": "stay alive music", "where": "youtube_music"}),
        ),
        (
            "open YouTube music app which is present in my laptop and open stay "
            "alive music of ReZero anime",
            (
                "play_youtube",
                {"query": "stay alive music of rezero anime", "where": "youtube_music"},
            ),
        ),
        (
            "play believer on youtube music",
            ("play_youtube", {"query": "believer", "where": "youtube_music"}),
        ),
        (
            "Jarvis played a song which is paused in my YouTube music.",
            ("system", {"action": "play_pause"}),
        ),
        (
            "Please open YouTube and play a vision quest trailer",
            ("play_youtube", {"query": "a vision quest trailer"}),
        ),
        ("play the next one", ("system", {"action": "next_track"})),
    ],
)
def test_music_commands(said: str, expected: tuple) -> None:
    assert _intent(said) == expected


def test_next_song_in_youtube_music_uses_its_queue() -> None:
    recent = Recent()
    recent.played(
        "stay alive", [("a", "Stay Alive"), ("b", "B")], 0, where="youtube_music"
    )
    assert _intent("next song", recent) == ("system", {"action": "next_track"})
    assert _intent("previous song", recent) == ("system", {"action": "previous_track"})
    assert "YouTube Music app" in recent.summary()


def test_songs_open_inside_the_installed_app(monkeypatch) -> None:
    from openjarvis.desktop import web

    launched: List[List[str]] = []
    closed: List[int] = []
    monkeypatch.setattr(
        web,
        "web_app",
        lambda name: ["proxy.exe", "--profile-directory=Default", "--app-id=yt"],
    )
    import openjarvis.desktop.winapi as winapi

    windows = [
        SimpleNamespace(
            hwnd=7, title="Stay Alive - YouTube Music", process="brave.exe"
        ),
        SimpleNamespace(hwnd=8, title="YouTube Music notes.txt", process="notepad.exe"),
    ]
    monkeypatch.setattr(winapi, "list_windows", lambda: windows)
    monkeypatch.setattr(winapi, "close_window", closed.append)
    monkeypatch.setattr(winapi, "start_detached", launched.append)
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    url = web.youtube_music_url("abc")
    assert web.open_in_web_app("YouTube Music", url)
    assert closed == [7]  # the old song's window, not Notepad
    assert launched == [
        [
            "proxy.exe",
            "--profile-directory=Default",
            "--app-id=yt",
            "--app-launch-url-for-shortcuts-menu-item=https://music.youtube.com/watch?v=abc",
        ]
    ]


def test_play_tool_routes_to_youtube_music(monkeypatch) -> None:
    from openjarvis.desktop import web

    opened: List[str] = []
    monkeypatch.setattr(web, "youtube_results", lambda q, n: [("v1", "Stay Alive")])
    monkeypatch.setattr(
        web, "open_in_web_app", lambda name, url: opened.append(url) or True
    )
    monkeypatch.setattr(server, "_mark_opening", lambda: None)
    monkeypatch.setattr(server.CONTEXT, "recent", Recent())
    reply = server.play_youtube("stay alive", where="youtube_music")
    assert reply == "Playing Stay Alive in YouTube Music."
    assert opened == ["https://music.youtube.com/watch?v=v1"]


# ---------------------------------------------------------------------------
# Web questions: limits and repaired arguments
# ---------------------------------------------------------------------------


def test_each_tool_has_a_per_request_limit() -> None:
    from openjarvis.desktop.graph import over_limit

    log = [{"name": "web_search"}] * 3 + [{"name": "browser_search"}]
    assert over_limit("web_search", log) is not None
    assert over_limit("browser_search", log) is None
    assert over_limit("keyboard", log * 5) is None  # no limit on keys


def test_arguments_outside_the_choices_are_repaired() -> None:
    from openjarvis.desktop.graph import fit_choices

    schemas = [
        {
            "function": {
                "name": "find_files",
                "parameters": {
                    "properties": {
                        "name": {"type": "string"},
                        "kind": {"enum": ["any", "folder", "file"]},
                    },
                    "required": ["name"],
                },
            }
        }
    ]
    assert fit_choices("find_files", {"name": "x", "kind": "app"}, schemas) == {
        "name": "x"
    }
    assert fit_choices("find_files", {"name": "x", "kind": "folders"}, schemas) == {
        "name": "x",
        "kind": "folder",
    }


def test_web_search_loop_is_cut_short() -> None:
    pytest.importorskip("langgraph")
    from openjarvis.desktop.graph import DesktopGraph
    from openjarvis.desktop.runtime import ToolOutcome

    calls: List[str] = []

    class Runtime:
        schemas: List[Dict[str, Any]] = []
        names = ["web_search", "browser_search"]

        def annotations(self, name):
            return None

        def call(self, name, args=None):
            calls.append(name)
            return ToolOutcome(name, f"results for {args}", True)

    def search(i: int) -> Dict[str, Any]:
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": f"c{i}",
                    "name": "web_search",
                    "arguments": json.dumps({"query": f"phones {i}"}),
                }
            ],
        }

    replies = [search(i) for i in range(6)] + [{"content": "Here are the phones."}]

    def model(messages, tools=None):
        return replies.pop(0)

    graph = DesktopGraph(generate=model, runtime=Runtime())
    state = graph.invoke("check best phones under 30000")
    assert calls.count("web_search") == 3
    assert state["reply"]


# ---------------------------------------------------------------------------
# Names for speech recognition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("saved", "said"),
    [
        ("Anand ECE-2", "Anand"),
        ("P Prathik ECE-2", "Prathik"),
        ("Krishna IT-1 Jr", "Krishna"),
        ("Keerthi SRT Deloitte", "Keerthi"),
        ("Chinna Ammamma", "Chinna Ammamma"),
        ("Kaushik Anna ECE-2 Sr", "Kaushik"),
        ("M.Neeraj Kumar", "Neeraj Kumar"),
        ("Sai Teja (Tammudu)", "Sai Teja"),
    ],
)
def test_how_saved_names_are_said(saved: str, said: str) -> None:
    assert spoken_name(saved) == said


def test_hints_put_starred_and_recent_contacts_first(tmp_path: Path) -> None:
    path = tmp_path / "contacts.txt"
    path.write_text(
        "[nicknames]\nMom = Amma\n\n[contacts]\nAamani Atta\nAnand ECE-2\n"
        "Thrisha ECE-2\nZakir ECE-2\nAmma\n",
        encoding="utf-8",
    )
    book = Directory(path, tmp_path / "cache.json")
    book.set_starred(["Thrisha ECE-2"])
    book.saw(["Zakir ECE-2", "Some Group"])
    hints = book.hint_names(limit_chars=40)
    assert hints[:3] == ["Mom", "Thrisha", "Zakir"]
    assert "Some Group" not in hints and sum(len(h) + 2 for h in hints) <= 40


@pytest.mark.parametrize(
    ("heard", "fixed"),
    [
        ("Please open on and in WhatsApp", "Please open Anand in WhatsApp"),
        ("Please open Anand.jar.", "Please open Anand chat."),
        ("call on and", "call Anand"),
        ("open minecraft.jar", "open minecraft.jar"),
        ("open the file and then close it", "open the file and then close it"),
        ("send it to him on WhatsApp", "send it to him on WhatsApp"),
    ],
)
def test_garbled_names_are_repaired(tmp_path: Path, heard: str, fixed: str) -> None:
    path = tmp_path / "contacts.txt"
    path.write_text("[contacts]\nAnand ECE-2\nThrisha ECE-2\n", encoding="utf-8")
    assert repair_names(heard, Directory(path, tmp_path / "cache.json")) == fixed


def test_starred_contacts_are_read_from_google_csv(tmp_path: Path) -> None:
    from openjarvis.desktop.contacts import read_contact_details

    source = tmp_path / "contacts.csv"
    source.write_text(
        "First Name,Middle Name,Last Name,Labels\n"
        "Amma,,,* myContacts ::: * starred\nDeepak,,,* myContacts\n",
        encoding="utf-8",
    )
    assert read_contact_details(source) == (["Amma", "Deepak"], {}, ["Amma"])


# ---------------------------------------------------------------------------
# jarvis voice
# ---------------------------------------------------------------------------


def test_voice_phrases_and_scoring() -> None:
    from openjarvis.cli.voice_cmd import build_phrases, name_heard, word_errors

    phrases = build_phrases(8, ["Anand", "Thrisha", "Daddy"])
    assert len(phrases) == 8
    assert phrases[0] == {"text": "Open Anand's chat on WhatsApp", "name": "Anand"}
    assert sum(1 for p in phrases if p["name"]) == 3
    assert word_errors("Open Anand's chat", "open anands chat") == (0, 3)
    assert word_errors("Set an alarm for six thirty", "set alarm for 6 30") == (1, 6)
    assert name_heard("Thrisha", "open thrisha's chat")
    assert not name_heard("Anand", "open armed")


def test_evaluate_picks_the_best_model_and_sets_it(tmp_path: Path, monkeypatch) -> None:
    from click.testing import CliRunner

    import openjarvis.speech.faster_whisper as fw
    from openjarvis.cli import voice_cmd

    folder = tmp_path / "voice" / "samples"
    folder.mkdir(parents=True)
    rows = [
        {"audio": f"{i}.wav", "text": "Open Anand's chat on WhatsApp", "name": "Anand"}
        for i in range(5)
    ]
    for row in rows:
        (folder / row["audio"]).write_bytes(b"RIFF")
    (folder / "samples.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8"
    )
    config = tmp_path / "config.toml"
    config.write_text(
        '[speech]\n# keep me\nmodel = "small.en"\ndevice = "cuda"\n', encoding="utf-8"
    )
    heard = {
        "small.en": "open armed chat on whatsapp",
        "medium.en": "open anand's chat on whatsapp",
    }

    class Backend:
        def __init__(self, model, device="auto", compute_type="int8"):
            self.model = model

        def transcribe(self, audio, **kwargs):
            return SimpleNamespace(text=heard[self.model])

    monkeypatch.setattr(fw, "FasterWhisperBackend", Backend)
    monkeypatch.setattr(voice_cmd, "samples_dir", lambda: folder)
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    result = CliRunner().invoke(
        voice_cmd.voice, ["evaluate", "--models", "small.en,medium.en", "--apply"]
    )
    assert result.exit_code == 0, result.output
    assert "Best on your voice: medium.en" in result.output
    assert "names 5/5" in result.output and "names 0/5" in result.output
    text = config.read_text(encoding="utf-8")
    assert 'model = "medium.en"' in text and "# keep me" in text


# ---------------------------------------------------------------------------
# WhatsApp search robustness, Gmail fast path, misheard "Jarvis"
# ---------------------------------------------------------------------------


def test_scan_retries_while_whatsapp_rerenders(monkeypatch) -> None:
    from openjarvis.desktop import whatsapp as wa

    attempts: List[int] = []

    def flaky(hwnd: int):
        attempts.append(hwnd)
        if len(attempts) < 3:
            raise OSError(
                -2147220991, "An event was unable to invoke any of the subscribers"
            )
        return "tree"

    monkeypatch.setattr(wa, "_scan_once", flaky)
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)
    assert wa._scan(7) == "tree" and len(attempts) == 3
    attempts.clear()
    monkeypatch.setattr(wa, "_scan_once", lambda h: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(wa.DesktopError, match="kept changing"):
        wa._scan(7)


def test_a_known_contact_without_a_chat_is_not_suggested_back(
    tmp_path: Path, monkeypatch
) -> None:
    import openjarvis.desktop.contacts as contacts_mod
    from openjarvis.desktop import whatsapp as wa

    path = tmp_path / "contacts.txt"
    path.write_text("[contacts]\nThrisha ECE-2\nHarsha ECE-2\n", encoding="utf-8")
    monkeypatch.setattr(
        contacts_mod, "_DIRECTORY", Directory(path, tmp_path / "cache.json")
    )
    window = SimpleNamespace(hwnd=1)
    tree = wa._Tree([ChatRow("Codefest", 0, "", (1, 1))], (5, 5), None, "", [])
    monkeypatch.setattr(wa, "_scan_window", lambda: (window, tree))
    monkeypatch.setattr(wa, "_search", lambda w, t, term: tree)
    monkeypatch.setattr(wa, "_clear_search", lambda w, t: None)
    with pytest.raises(wa.DesktopError) as error:
        wa.open_chat("Thrisha")
    message = str(error.value)
    assert message.startswith("Thrisha ECE-2 is in your contacts, but WhatsApp")
    assert "Or did you mean Harsha ECE-2" in message
    assert message.count("Thrisha ECE-2") == 1


def test_check_my_mails_is_one_fast_call() -> None:
    found = match("Darv, check my mails.", tools={"gmail_unread"})
    assert (found.tool, found.args) == ("gmail_unread", {})


def test_misheard_jarvis_still_addresses_jarvis() -> None:
    from openjarvis.cli._assistant import classify_utterance

    assert (
        classify_utterance("Darv, what is the weather", in_conversation=False)
        != "ignore"
    )
    assert classify_utterance("Javis open notepad", in_conversation=False) == "command"
    assert (
        classify_utterance("Darwin was a scientist", in_conversation=False) == "ignore"
    )


# ---------------------------------------------------------------------------
# WhatsApp: the right person, even off screen, and a clean search box after
# ---------------------------------------------------------------------------

CLASS = [
    ChatRow(t, 0, "", (100, 200 + 40 * i))
    for i, t in enumerate(
        ["Siri ECE-2", "Harsha ECE-2", "Harsh K ECE-2", "Priya ECE-2"]
    )
]


def test_a_shared_tag_never_makes_two_people_the_same() -> None:
    from openjarvis.desktop.contacts import score_name

    # "ECE-2" is in both names; the people are different.
    assert best_chat(CLASS, "Thrisha ECE-2") is None
    assert best_chat(CLASS, "Sarika ECE-2") is None
    assert score_name("sarika ece 2", "Siri ECE-2") < 0.6
    assert score_name("thrisha ece 2", "Harsha ECE-2") < 0.8
    # Misheard forms of the right name still find it.
    rows = CLASS + [ChatRow("Thrisha ECE-2", 0, "", (100, 400))]
    assert best_chat(rows, "Trisha").title == "Thrisha ECE-2"
    assert best_chat(rows, "Trisha ECE-2").title == "Thrisha ECE-2"
    assert best_chat(CLASS, "Harsha ECE-2").title == "Harsha ECE-2"


def _class_book(tmp_path: Path, monkeypatch) -> Directory:
    import openjarvis.desktop.contacts as contacts_mod

    path = tmp_path / "contacts.txt"
    path.write_text(
        "[contacts]\nSarika ECE-2\nSiri ECE-2\nThrisha ECE-2\nHarsha ECE-2\n",
        encoding="utf-8",
    )
    book = Directory(path, tmp_path / "cache.json")
    monkeypatch.setattr(contacts_mod, "_DIRECTORY", book)
    return book


def test_one_saved_contact_is_never_swapped_for_another(
    tmp_path: Path, monkeypatch
) -> None:
    from openjarvis.desktop import whatsapp as wa

    book = _class_book(tmp_path, monkeypatch)
    siri = ChatRow("Siri ECE-2", 0, "", (1, 1))
    monkeypatch.setattr(wa, "best_chat", lambda rows, contact: siri)
    assert wa._chat_for([siri], "Sarika ECE-2") is None
    assert wa._chat_for([siri], "Siri") is siri  # not a saved name: fine
    # Nor remembered as an alias, and an old wrong alias is ignored.
    book.learn("Sarika ECE-2", "Siri ECE-2")
    assert "sarika ece 2" not in book.learned
    book.learned["sarika ece 2"] = "Siri ECE-2"
    assert book.alias("Sarika ECE-2") is None
    assert book.resolve("Sarika ECE-2") == "Sarika ECE-2"


def _fake_whatsapp(monkeypatch, tree, rescans):
    """open_chat against scripted scans; returns the list of clicks."""
    from openjarvis.desktop import whatsapp as wa
    from openjarvis.desktop import winapi

    clicks: List[tuple] = []
    keys: List[str] = []
    window = SimpleNamespace(hwnd=1)
    monkeypatch.setattr(wa, "COM", SimpleNamespace(run=lambda fn, timeout=0: fn()))
    monkeypatch.setattr(wa, "_scan_window", lambda: (window, tree))
    scans = iter(rescans)
    monkeypatch.setattr(wa, "_scan", lambda hwnd: next(scans))
    import time as real_time

    clock = SimpleNamespace(sleep=lambda s: None, monotonic=real_time.monotonic)
    monkeypatch.setattr(wa, "time", clock)  # this module only
    monkeypatch.setattr(
        winapi, "click_in", lambda hwnd, x, y, **k: clicks.append((x, y))
    )
    monkeypatch.setattr(
        winapi, "press_hotkey", lambda combo, times=1: keys.append(combo)
    )
    monkeypatch.setattr(
        winapi, "type_text", lambda text, **k: keys.append(f"type:{text}")
    )
    return clicks, keys


def test_an_offscreen_chat_is_scrolled_into_view_before_the_click(
    tmp_path: Path, monkeypatch
) -> None:
    from openjarvis.desktop import whatsapp as wa

    _class_book(tmp_path, monkeypatch)
    hidden = ChatRow("Thrisha ECE-2", 0, "", (300, 1784), offscreen=True)
    shown = ChatRow("Thrisha ECE-2", 0, "", (300, 640))
    tree = wa._Tree(CLASS + [hidden], (350, 113), None, "", [])
    after_scroll = wa._Tree(CLASS + [shown], (350, 113), None, "", [])
    opened = wa._Tree(CLASS + [shown], (350, 113), (900, 990), "Thrisha ECE-2", [])
    scrolled: List[str] = []
    monkeypatch.setattr(
        wa, "_scroll_into_view", lambda hwnd, title: scrolled.append(title) or True
    )
    clicks, keys = _fake_whatsapp(monkeypatch, tree, [after_scroll, opened])
    assert wa.open_chat("Trisha") == "Opened your WhatsApp chat with Thrisha ECE-2."
    assert scrolled == ["Thrisha ECE-2"]
    assert clicks == [(300, 640)]  # where it is now, not where it was
    assert keys == []  # found in the list: nothing typed


def test_a_chat_that_never_scrolls_into_view_is_not_clicked(monkeypatch) -> None:
    from openjarvis.desktop import whatsapp as wa

    hidden = ChatRow("Thrisha ECE-2", 0, "", (300, 1784), offscreen=True)
    still = wa._Tree([hidden], None, None, "", [])
    clicks, _ = _fake_whatsapp(monkeypatch, still, [still] * 50)
    monkeypatch.setattr(wa, "_scroll_into_view", lambda hwnd, title: False)
    ticks = iter(range(0, 1000))
    wa.time.monotonic = lambda: next(ticks)
    with pytest.raises(wa.DesktopError, match="couldn't scroll to it"):
        wa._reveal(SimpleNamespace(hwnd=1), hidden)
    assert clicks == []


def test_the_search_box_is_found_even_with_text_in_it() -> None:
    from openjarvis.desktop import whatsapp as wa

    # Unnamed while it holds "Thrisha ECE-2"; the composer is lower down.
    edits = [(133, 103, 568, 123), (744, 982, 1857, 1006)]
    assert wa._unnamed_search_box(edits, 982, 600) == ((350, 113), 103)
    assert wa._unnamed_search_box(edits[1:], 982, 600) == (None, 0)
    assert wa._unnamed_search_box([], 10**6, 0) == (None, 0)


@pytest.mark.parametrize(
    "title, heading",
    [
        ("Chats", True),
        ("Messages", True),
        ("Groups in common", True),
        ("Contacts", True),
        ("Chatterjee", False),
        ("Thrisha ECE-2", False),
    ],
)
def test_section_headings_are_not_chats(title: str, heading: bool) -> None:
    from openjarvis.desktop import whatsapp as wa

    assert bool(wa._SECTION.match(title)) is heading


def test_nothing_is_typed_unless_the_search_box_has_focus(monkeypatch) -> None:
    from openjarvis.desktop import whatsapp as wa

    tree = wa._Tree([], (350, 113), (900, 990), "Amma", [])
    clicks, keys = _fake_whatsapp(monkeypatch, tree, [])
    monkeypatch.setattr(wa, "_focus_search", lambda window, point: False)
    with pytest.raises(wa.DesktopError, match="search box"):
        wa._search(SimpleNamespace(hwnd=1), tree, "Thrisha")
    wa._clear_search(SimpleNamespace(hwnd=1), tree)
    assert keys == []  # no Ctrl+A that could wipe a draft message


def test_clearing_the_search_never_presses_escape(monkeypatch) -> None:
    from openjarvis.desktop import whatsapp as wa

    tree = wa._Tree([], (350, 113), (900, 990), "Thrisha ECE-2", [])
    clicks, keys = _fake_whatsapp(monkeypatch, tree, [])
    monkeypatch.setattr(wa, "_focus_search", lambda window, point: True)
    wa._clear_search(SimpleNamespace(hwnd=1), tree)
    assert keys == ["ctrl+a", "backspace"]  # Esc would close the open chat
    assert clicks == [(900, 990)]  # back in the message box


def test_a_search_keeps_the_box_position_for_the_next_term(monkeypatch) -> None:
    from openjarvis.desktop import whatsapp as wa

    tree = wa._Tree([], (350, 113), None, "", [])
    results = wa._Tree([ChatRow("Thrisha ECE-2", 0, "", (1, 1))], None, None, "", [])
    _, keys = _fake_whatsapp(monkeypatch, tree, [results] * 3)
    monkeypatch.setattr(wa, "_focus_search", lambda window, point: True)
    found = wa._search(SimpleNamespace(hwnd=1), tree, "Thrisha")
    assert found.search == (350, 113)
    assert keys == ["ctrl+a", "type:Thrisha"]


def test_a_say_which_question_goes_to_the_user_not_the_model() -> None:
    pytest.importorskip("langgraph")
    from tests.desktop.test_graph import FakeRuntime, ScriptedModel, _call, _graph

    model = ScriptedModel(
        [
            {"content": "", "tool_calls": [_call("whatsapp", action="open")]},
            # What the 4B model did next: open a suggestion by itself.
            {"content": "", "tool_calls": [_call("whatsapp", action="open")]},
        ]
    )
    # No "couldn't"/"didn't" in it, and still not dressed up as a failure.
    question = "FAIL There's more than one 'Krishna'. Did you mean A or B? Say which."
    runtime = FakeRuntime({"whatsapp": question})
    state = _graph(model, runtime).invoke("open thrisha's chat on whatsapp")
    assert state["reply"] == question
    assert len(runtime.calls) == 1 and len(model.seen) == 1


def test_whatsapp_is_read_again_until_its_page_is_ready(monkeypatch) -> None:
    import time as real_time

    from openjarvis.desktop import whatsapp as wa

    window = SimpleNamespace(hwnd=1)
    blank = wa._Tree([], None, None, "", [])  # just brought to the front
    rows = [ChatRow("Amma", 0, "", (1, 1)), ChatRow("Anand ECE-2", 0, "", (1, 2))]
    ready = wa._Tree(rows, (376, 113), None, "", [])
    reads = iter([blank, blank, ready, ready])
    count: List[int] = []

    def scan(hwnd: int):
        count.append(hwnd)
        return next(reads)

    monkeypatch.setattr(wa, "ensure_window", lambda: window)
    monkeypatch.setattr(wa, "COM", SimpleNamespace(run=lambda fn, timeout=0: fn()))
    monkeypatch.setattr(wa, "_scan", scan)
    clock = SimpleNamespace(sleep=lambda s: None, monotonic=real_time.monotonic)
    monkeypatch.setattr(wa, "time", clock)
    _, tree = wa._scan_window()
    assert tree is ready and len(count) == 4  # waited for a settled list
    # Ready on the first read: no waiting at all.
    reads = iter([ready])
    count.clear()
    _, tree = wa._scan_window()
    assert tree is ready and len(count) == 1


def test_no_search_means_no_blaming_whatsapps_search(
    tmp_path: Path, monkeypatch
) -> None:
    from openjarvis.desktop import whatsapp as wa

    _class_book(tmp_path, monkeypatch)
    blank = wa._Tree([], None, None, "", [])
    _fake_whatsapp(monkeypatch, blank, [])
    with pytest.raises(wa.DesktopError, match="WhatsApp is still loading") as error:
        wa.open_chat("Thrisha")
    assert "search didn't show" not in str(error.value)


SAY_WHICH = {
    "tool": "whatsapp",
    "args": {"action": "open_chat", "contact": "Anand"},
    "text": (
        "Anand ECE-2 is in your contacts, but WhatsApp's search didn't show a "
        "chat with them, so I didn't open anything. Or did you mean Nanda Sai "
        "ECE-2 or Nandu? Say which."
    ),
}


@pytest.mark.parametrize(
    "said, contact",
    [
        ("I want Anand CSC2", "Anand CSC2"),
        ("the second one", "Nandu"),
        ("I want the first one", "Nanda Sai ECE-2"),
        ("Nandu", "Nandu"),
        ("try again", "Anand"),
        ("I want to play music", None),  # a new request, not a name
        ("okay", None),
    ],
)
def test_answers_to_say_which_skip_the_model(said: str, contact: str | None) -> None:
    from openjarvis.desktop.fastpath import _retry_after_error

    found = _retry_after_error(said, said.lower(), SAY_WHICH)
    assert (found.args["contact"] if found else None) == contact
