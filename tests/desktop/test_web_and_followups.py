"""Opening real pages, music to YouTube Music, and the follow-up fixes from a
long voice session (interruptions, "Ammatak", "Thank you.", guard notes)."""

from __future__ import annotations

import json
import time as real_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from openjarvis.desktop import server, web
from openjarvis.desktop.fastpath import FastIntent, match
from openjarvis.desktop.recent import Recent, links_in

# ---------------------------------------------------------------------------
# YouTube search: both result formats
# ---------------------------------------------------------------------------


def _page(data: Dict[str, Any]) -> str:
    return f"<script>var ytInitialData = {json.dumps(data)};</script>"


OLD_FORMAT = {
    "contents": [
        {
            "videoRenderer": {
                "videoId": "AAAAAAAAAAA",
                "title": {"runs": [{"text": "Old one"}]},
            }
        },
        {
            "videoRenderer": {
                "videoId": "BBBBBBBBBBB",
                "title": {"runs": [{"text": "Old two"}]},
            }
        },
    ]
}
VIDEO, PLAYLIST = "LOCKUP_CONTENT_TYPE_VIDEO", "LOCKUP_CONTENT_TYPE_PLAYLIST"
NEW_FORMAT = {
    "contents": [
        {"adSlotRenderer": {"videoRenderer": {"videoId": "ADADADADADA"}}},
        {
            "reelShelfRenderer": {
                "items": [
                    {"lockupViewModel": {"contentId": "SHORTSHORTS",
                                         "contentType": VIDEO}}
                ]
            }
        },
        {
            "lockupViewModel": {
                "contentId": "DHHWzoRBjlU",
                "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                "metadata": {
                    "lockupMetadataViewModel": {
                        "title": {"content": "OnePlus Nord CE 6 Lite Detailed Review"}
                    }
                },
            }
        },
        {"lockupViewModel": {"contentId": "PLxyz", "contentType": PLAYLIST}},
    ]
}  # fmt: skip


def test_both_youtube_result_formats_give_videos() -> None:
    assert web._results_from_data(_page(OLD_FORMAT), 5) == [
        ("AAAAAAAAAAA", "Old one"),
        ("BBBBBBBBBBB", "Old two"),
    ]
    # The newer format, which made half of all searches "no direct match";
    # ads, Shorts and playlists are skipped.
    assert web._results_from_data(_page(NEW_FORMAT), 5) == [
        ("DHHWzoRBjlU", "OnePlus Nord CE 6 Lite Detailed Review")
    ]


@pytest.mark.parametrize(
    "site, start",
    [
        ("amazon.in", "https://www.amazon.in/s?k="),
        ("www.amazon.in", "https://www.amazon.in/s?k="),
        ("flipkart.com", "https://www.flipkart.com/search?q="),
        ("amazon", "https://www.amazon.in/s?k="),
        ("gsmarena.com", "https://www.google.com/search?q="),  # no own search
    ],
)
def test_a_shop_domain_opens_that_shops_own_search(site: str, start: str) -> None:
    assert web.search_url("OnePlus Nord CE6 Lite", site).startswith(start)


# ---------------------------------------------------------------------------
# Music goes to YouTube Music
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_text, query, where",
    [
        ("Jarvis, please play Durandar songs", "durandar songs", "youtube_music"),
        (
            "Please play Dhranda songs in YouTube music, not in YouTube website.",
            "dhranda songs",
            "youtube_music",
        ),
        ("play lofi music", "lofi", "youtube_music"),
        ("play arijit singh songs on the youtube website", "arijit songs", "youtube"),
        (
            "Open the YouTube video review of the Nord CE 6 Lite",
            "nord review",
            "youtube",
        ),
        ("play the Dhurandhar trailer", "dhurandhar trailer", "youtube"),
    ],
)
def test_music_requests_go_to_youtube_music(
    monkeypatch, request_text: str, query: str, where: str
) -> None:
    monkeypatch.setattr(server.CONTEXT, "request", request_text)
    assert server._destination(query, "youtube") == where


def test_a_song_with_no_match_searches_youtube_music_not_youtube(monkeypatch) -> None:
    opened: List[str] = []
    monkeypatch.setattr(server.CONTEXT, "request", "play some obscure song")
    monkeypatch.setattr(server.CONTEXT, "recent", Recent())
    monkeypatch.setattr(web, "youtube_results", lambda q, n: [])
    monkeypatch.setattr(
        web, "open_in_web_app", lambda name, url: opened.append(url) or True
    )
    monkeypatch.setattr(server, "_mark_opening", lambda: None)
    reply = server.play_youtube("obscure song")
    assert reply == "No direct match, so I searched YouTube Music for obscure song."
    assert opened == ["https://music.youtube.com/search?q=obscure+song"]


# ---------------------------------------------------------------------------
# open_page: the real pages, not a search page
# ---------------------------------------------------------------------------

AMAZON_RESULTS = """### OnePlus Nord CE6 Lite | 8GB+128GB | Vivid Mint
Source: https://www.amazon.in/OnePlus-Segments-Dimensity/dp/B0GVYXPZBS
Summary: ...

### OnePlus Nord CE6 Lite: Electronics
Source: https://www.amazon.in/b?ie=UTF8&node=217693948031
Summary: ...

### one plus nord ce 6 lite - Amazon.in
Source: https://www.amazon.in/s?k=one+plus+nord+ce+6+lite
Summary: ...

### Nord CE6 Lite review
Source: https://www.gsmarena.com/oneplus_nord_ce6_lite-review.php
Summary: ...
"""

PHONE_RESULTS = """### Best camera phones under 30000
Source: https://www.91mobiles.com/list-of-phones/top-10-camera-mobiles-below-30000
Summary: a

### Best Camera Phones Under 30000 - Beebom
Source: https://gadgets.beebom.com/best-camera-phones-under-30000
Summary: b

### Google results
Source: https://www.google.com/search?q=phones
Summary: c

### Phones under 30000 - Gadgets Now
Source: https://gadgetsnow.indiatimes.com/best-products/smartphone/best-camera
Summary: d
"""


@pytest.fixture
def opened(monkeypatch) -> List[str]:
    urls: List[str] = []
    monkeypatch.setattr(server.os, "startfile", urls.append, raising=False)
    monkeypatch.setattr(server, "_mark_opening", lambda: None)
    monkeypatch.setattr(server.CONTEXT, "recent", Recent())
    return urls


def test_the_amazon_page_of_a_phone_is_its_product_page(monkeypatch, opened) -> None:
    searched: List[str] = []
    monkeypatch.setattr(
        server.CONTEXT,
        "link_search",
        lambda q, n: searched.append(q) or AMAZON_RESULTS,
    )
    reply = server.open_page("OnePlus Nord CE6 Lite", site="amazon")
    assert searched == ["OnePlus Nord CE6 Lite site:amazon.in"]
    assert opened == ["https://www.amazon.in/OnePlus-Segments-Dimensity/dp/B0GVYXPZBS"]
    assert reply.startswith("Opened OnePlus Nord CE6 Lite | 8GB+128GB")


def test_open_the_first_three_websites_of_the_last_search(monkeypatch, opened) -> None:
    recent = server.CONTEXT.recent
    recent.note("web_search", {"query": "phones under 30000"}, PHONE_RESULTS, True)
    monkeypatch.setattr(server.CONTEXT, "link_search", None)
    reply = server.open_page(count=3)
    assert opened == [
        "https://www.91mobiles.com/list-of-phones/top-10-camera-mobiles-below-30000",
        "https://gadgets.beebom.com/best-camera-phones-under-30000",
        "https://gadgetsnow.indiatimes.com/best-products/smartphone/best-camera",
    ]  # never the Google search page
    assert reply.startswith("Opened 3 pages: 1. Best camera phones under 30000")


def test_after_a_browser_search_the_same_query_finds_real_pages(
    monkeypatch, opened
) -> None:
    recent = server.CONTEXT.recent
    recent.note(
        "browser_search",
        {"query": "OnePlus Nord CE6 Lite", "site": "amazon.in"},
        "Searching amazon.in for OnePlus Nord CE6 Lite.",
        True,
    )
    searched: List[str] = []
    monkeypatch.setattr(
        server.CONTEXT, "link_search", lambda q, n: searched.append(q) or AMAZON_RESULTS
    )
    server.open_page(count=1)
    assert searched == ["OnePlus Nord CE6 Lite site:amazon.in"]
    assert opened[0].endswith("/dp/B0GVYXPZBS")


def test_web_results_are_remembered_for_follow_ups() -> None:
    assert links_in(PHONE_RESULTS)[0] == (
        "Best camera phones under 30000",
        "https://www.91mobiles.com/list-of-phones/top-10-camera-mobiles-below-30000",
    )
    recent = Recent()
    recent.note("web_search", {"query": "phones"}, PHONE_RESULTS, True)
    assert len(recent.links) == 4 and "open_page" in recent.summary()


@pytest.mark.parametrize(
    "said, intent",
    [
        ("proceed to open the first three websites.", ("open_page", {"count": 3})),
        ("open the top 2 links", ("open_page", {"count": 2})),
        ("open the first website", ("open_page", {"count": 1})),
    ],
)
def test_open_the_first_websites_is_one_fast_step(said: str, intent: tuple) -> None:
    recent = Recent()
    recent.note("web_search", {"query": "phones"}, PHONE_RESULTS, True)
    found = match(said, recent=recent, tools={"open_page"})
    assert isinstance(found, FastIntent) and (found.tool, found.args) == intent


def test_open_the_first_video_plays_it() -> None:
    recent = Recent()
    recent.played("nord ce 6 lite review", [("a", "A"), ("b", "B")], 0)
    found = match("Please open the first video.", recent=recent, tools={"play_youtube"})
    assert (found.tool, found.args) == ("play_youtube", {"which": 1})


# ---------------------------------------------------------------------------
# The agent loop: guessed clicks, internal notes, interruptions, facts
# ---------------------------------------------------------------------------


def test_clicks_at_guessed_spots_are_refused() -> None:
    from openjarvis.desktop.graph import blind_click

    click = {"action": "click", "x": 500, "y": 300}
    assert blind_click("mouse", click, [], "Please open the first video.")
    # A description gives no positions: the model clicked guesses after one
    # four times and paused the trailer.
    described = [{"name": "screen", "args": {"action": "describe"}, "ok": True}]
    assert blind_click("mouse", click, described, "open the third video")
    located = [{"name": "screen", "args": {"action": "locate"}, "ok": True}]
    assert blind_click("mouse", click, located, "open the first video") is None
    assert blind_click("mouse", click, [], "click the middle of the screen") is None
    assert blind_click("mouse", {"action": "scroll_down"}, [], "scroll") is None


def test_internal_notes_never_reach_the_user() -> None:
    from openjarvis.desktop.graph import honest_reply

    log = [
        {"name": "browser_search", "ok": True, "text": "Searching amazon.in for x."},
        {
            "name": "browser_search",
            "ok": False,
            "text": "Not done: browser_search was already used 2 times for this "
            "request. Answer the user now with what you have.",
        },
    ]
    reply = "I opened Amazon's search for the OnePlus Nord CE6 Lite."
    assert honest_reply(reply, log) == reply
    only_refused = log[1:]
    assert (
        honest_reply("Done, I've called them.", only_refused) == "I couldn't do that."
    )


def test_an_interrupted_step_is_closed_not_redone(tmp_path: Path) -> None:
    pytest.importorskip("langgraph")
    from tests.desktop.test_graph import FakeRuntime, ScriptedModel, _call, _graph

    class Interrupting(FakeRuntime):
        def call(self, name: str, args: Dict[str, Any] | None = None):
            if (args or {}).get("contact") == "Ammatak":
                self.calls.append((name, args))
                raise KeyboardInterrupt  # the user spoke over Jarvis
            return super().call(name, args)

    model = ScriptedModel(
        [
            {"content": "", "tool_calls": [_call("whatsapp", contact="Ammatak")]},
            {"content": "", "tool_calls": [_call("whatsapp", contact="Amma")]},
            {"content": "Opened Amma."},
        ]
    )
    graph = _graph(model, Interrupting(), tmp_path)
    with pytest.raises(KeyboardInterrupt):
        graph.invoke("Open Ammatak.")
    graph.invoke("Open Amma.")
    # What the model saw when it handled "Open Amma.": the interrupted call
    # has a result saying so, right before the new request.
    seen = [(m.role.value, m.content or "") for m in model.seen[1]]
    asked = seen.index(("user", "Open Amma."))
    assert seen[asked - 2][0] == "tool" and "interrupted" in seen[asked - 2][1]
    assert seen[asked - 1] == ("assistant", "(Stopped.)")


def test_a_new_call_waits_for_an_interrupted_one() -> None:
    import concurrent.futures
    import threading

    from openjarvis.desktop.runtime import ToolRuntime

    order: List[str] = []

    class Server:
        async def call_tool(self, name: str, args: Dict[str, Any]):
            order.append(name)
            return SimpleNamespace(content=[SimpleNamespace(text=name)])

    runtime = ToolRuntime(Server())
    try:
        busy: concurrent.futures.Future = concurrent.futures.Future()
        runtime._busy = busy  # the open_chat('Ammatak') still running

        def finish() -> None:
            real_time.sleep(0.2)
            order.append("old call finished")
            busy.set_result(None)

        threading.Thread(target=finish).start()
        assert runtime.call("whatsapp").text == "whatsapp"
        assert order == ["old call finished", "whatsapp"]
    finally:
        runtime.close()


def test_buying_advice_numbers_are_checked() -> None:
    from openjarvis.desktop.graph import _FACTS, ungrounded_numbers

    assert _FACTS.search("suggest me which one to buy")
    sources = ["OnePlus Nord CE6 Lite ₹30,999.00 In stock"]
    assert ungrounded_numbers("Around ₹23,999", sources) == ["₹23,999"]
    assert ungrounded_numbers("₹30,999 on Amazon", sources) == []


# ---------------------------------------------------------------------------
# Names and thanks
# ---------------------------------------------------------------------------


def test_names_run_together_with_chat_are_split() -> None:
    from openjarvis.desktop.contacts import unfuse_chat

    assert unfuse_chat("Ammatak") == "Amma"
    assert unfuse_chat("Anandchat") == "Anand"
    assert unfuse_chat("Amma") == ""
    assert unfuse_chat("Satak") == ""  # too short to be a name + "tak"


def test_amman_is_amma_not_another_contact(tmp_path: Path) -> None:
    from openjarvis.desktop.contacts import Directory

    path = tmp_path / "contacts.txt"
    path.write_text(
        "[contacts]\nAamani Atta\nAmma\nAmma (Office Number)\n"
        "Amma (office phone number)\nKrishna Kaushik\nKrishna Sameer\n",
        encoding="utf-8",
    )
    book = Directory(path, tmp_path / "cache.json")
    assert book.resolve("Amman") == "Amma"
    assert book.resolve("Amma") == "Amma"
    assert book.resolve("Krishna") is None  # a real tie still asks


def test_open_ammatak_opens_ammas_chat(tmp_path: Path, monkeypatch) -> None:
    import openjarvis.desktop.contacts as contacts_mod
    from openjarvis.desktop import whatsapp as wa
    from openjarvis.desktop.contacts import Directory
    from openjarvis.desktop.whatsapp import ChatRow

    path = tmp_path / "contacts.txt"
    path.write_text("[contacts]\nAmma\nAnand ECE-2\n", encoding="utf-8")
    monkeypatch.setattr(
        contacts_mod, "_DIRECTORY", Directory(path, tmp_path / "cache.json")
    )
    window = SimpleNamespace(hwnd=1)
    rows = [
        ChatRow("Amma", 0, "", (100, 300)),
        ChatRow("Anand ECE-2", 0, "", (100, 360)),
    ]
    tree = wa._Tree(rows, (350, 113), None, "", [])
    opened = wa._Tree(rows, (350, 113), (900, 990), "Amma", [])
    clicks: List[tuple] = []
    monkeypatch.setattr(wa, "_scan_window", lambda: (window, tree))
    monkeypatch.setattr(wa, "_wait_for_recipient", lambda w, c: opened)
    from openjarvis.desktop import winapi

    monkeypatch.setattr(winapi, "click_in", lambda h, x, y, **k: clicks.append((x, y)))
    assert wa.open_chat("Ammatak") == "Opened your WhatsApp chat with Amma."
    assert clicks == [(100, 300)]


@pytest.mark.parametrize(
    "said, kind",
    [
        ("Thank you.", "ignore"),
        ("Okay, thank you so much.", "ignore"),
        ("Thanks Jarvis", "ignore"),
        ("Anand", "command"),  # a real answer after a failure
        ("yes", "command"),
    ],
)
def test_thanks_is_never_taken_as_a_correction(said: str, kind: str) -> None:
    from openjarvis.cli._assistant import classify_utterance

    assert classify_utterance(said, in_conversation=True, expecting=True) == kind


# ---------------------------------------------------------------------------
# WhatsApp in a background tab, and a slow first load
# ---------------------------------------------------------------------------


def test_a_background_whatsapp_tab_is_reused(monkeypatch) -> None:
    from openjarvis.desktop import whatsapp as wa
    from openjarvis.desktop import winapi

    brave = SimpleNamespace(hwnd=4, title="YouTube - Brave", process="brave.exe")
    found = SimpleNamespace(hwnd=4, title="WhatsApp - Brave", process="brave.exe")
    switched: List[int] = []
    monkeypatch.setattr(winapi, "list_windows", lambda: [brave])
    monkeypatch.setattr(wa, "COM", SimpleNamespace(run=lambda fn, timeout=0: fn()))
    monkeypatch.setattr(wa, "_whatsapp_tab", lambda hwnd: switched.append(hwnd) or True)
    finds = iter([None, found])
    monkeypatch.setattr(wa, "find_window", lambda: next(finds))
    monkeypatch.setattr(wa.time, "sleep", lambda s: None)
    assert wa._from_background_tab() is found and switched == [4]


def test_a_just_started_whatsapp_gets_longer_to_load(monkeypatch) -> None:
    from openjarvis.desktop import whatsapp as wa
    from openjarvis.desktop.whatsapp import ChatRow

    window = SimpleNamespace(hwnd=1)
    blank = wa._Tree([], None, None, "", [])
    ready = wa._Tree([ChatRow("Amma", 0, "", (1, 1))], (5, 5), None, "", [])
    now = [0.0]

    def scan(hwnd: int):
        return ready if now[0] > 20 else blank  # chats show up after ~20 s

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(wa, "ensure_window", lambda: window)
    monkeypatch.setattr(wa, "COM", SimpleNamespace(run=lambda fn, timeout=0: fn()))
    monkeypatch.setattr(wa, "_scan", scan)
    monkeypatch.setattr(
        wa, "time", SimpleNamespace(sleep=sleep, monotonic=lambda: now[0])
    )
    monkeypatch.setattr(wa, "_LAUNCHED_AT", 0.0)
    _, tree = wa._scan_window()
    assert tree is ready
    # Already open (not just started): the short wait applies.
    now[0] = 1000.0
    monkeypatch.setattr(wa, "_scan", lambda hwnd: blank)
    _, tree = wa._scan_window()
    assert tree is blank and now[0] < 1010


# ---------------------------------------------------------------------------
# "The third video on the right side" of the YouTube page
# ---------------------------------------------------------------------------

W = "https://www.youtube.com/watch?v="
# What the trailer's page looked like: comment timestamps link to the video
# that's playing; the suggestions column is on the right.
RIGHT = (1706, 1889)  # the suggestions column's x range


def _at(x: tuple, top: int, height: int = 40) -> tuple:
    return (x[0], top, x[1], top + height)


DOOM = "Every HERO Who Can DEFEAT Doctor Doom 10 minutes, 6 seconds"
PAGE_LINKS = [
    ("1 month ago", W + "sXKnmgmbkoE&lc=Ugwx5", (177, 1275, 245, 1295), True),
    ("2:25", W + "sXKnmgmbkoE&t=145s", (68, 1962, 95, 1980), True),
    ("NIGHTCRAWLER For Noobs 17 minutes", W + "trnN5moNLVU", _at(RIGHT, 226), False),
    ("NIGHTCRAWLER thumbnail", W + "trnN5moNLVU", (1370, 186, 1690, 366), False),
    (DOOM, W + "Pm9I8Rwfh6Q&pp=x", _at(RIGHT, 416, 60), False),
    (
        "VISIONQUEST EPISODE 7 BREAKDOWN!",
        W + "PpWDK-t-8VU&pp=y",
        _at(RIGHT, 607, 60),
        False,
    ),
    ("EVERYTHING IN 7.3!!!", W + "aaaaaaaaaaa", _at(RIGHT, 798, 60), False),
    ("Main column video", W + "bbbbbbbbbbb", (40, 1500, 600, 1540), True),
]


def test_the_third_video_on_the_right_is_the_third_suggestion() -> None:
    from openjarvis.desktop.youtube_page import pick

    right = pick(PAGE_LINKS, "sXKnmgmbkoE", 1936, "right")
    assert [v.title for v in right[:3]] == [
        "NIGHTCRAWLER For Noobs",
        "Every HERO Who Can DEFEAT Doctor Doom",
        "VISIONQUEST EPISODE 7 BREAKDOWN!",
    ]  # one entry per video, never the one playing, lengths trimmed
    assert right[2].center == (1797, 637)
    everything = pick(PAGE_LINKS, "sXKnmgmbkoE", 1936, "")
    assert "Main column video" in [v.title for v in everything]


@pytest.mark.parametrize(
    "said, which",
    [
        ("Open the third video in the left side, right side.", 3),
        ("Weigh the third video which is present in the right side of the panel.", 3),
        ("play the second suggestion", 2),
        ("open the 4th video on the right", 4),
    ],
)
def test_the_nth_video_on_the_page_is_one_fast_step(said: str, which: int) -> None:
    recent = Recent()
    recent.played("vision quest trailer", [("a", "A"), ("b", "B"), ("c", "C")], 0)
    found = match(said, recent=recent, tools={"play_youtube"})
    assert (found.tool, found.args) == (
        "play_youtube",
        {"which": which, "on_page": True},
    )
    # Not the third result of a new search, and nothing for other lists.
    assert match("open the second chat on the left", tools={"play_youtube"}) is None


def test_the_page_video_is_clicked_where_it_is_and_checked(monkeypatch) -> None:
    from openjarvis.desktop import winapi
    from openjarvis.desktop import youtube_page as yp

    titles = iter(
        ["VisionQuest Trailer - YouTube - Brave"] * 2 + ["Breakdown - YouTube"]
    )
    window = SimpleNamespace(hwnd=3, title="VisionQuest Trailer - YouTube - Brave")
    monkeypatch.setattr(
        yp, "find_window", lambda: SimpleNamespace(hwnd=3, title=next(titles))
    )
    monkeypatch.setattr(yp, "COM", SimpleNamespace(run=lambda fn, timeout=0: fn()))
    monkeypatch.setattr(yp, "_read", lambda hwnd: ("sXKnmgmbkoE", 1936, PAGE_LINKS))
    monkeypatch.setattr(
        yp, "time", SimpleNamespace(sleep=lambda s: None, monotonic=real_time.monotonic)
    )
    clicks: List[tuple] = []
    monkeypatch.setattr(winapi, "focus_window", lambda hwnd: None)
    monkeypatch.setattr(winapi, "click_in", lambda h, x, y, **k: clicks.append((x, y)))
    del window
    reply, videos, index = yp.play(3, "right")
    assert clicks == [(1797, 637)] and index == 2
    assert (
        reply
        == "Playing VISIONQUEST EPISODE 7 BREAKDOWN! (the 3rd video on the right)."
    )


def test_play_tool_uses_the_page_and_remembers_the_list(monkeypatch) -> None:
    from openjarvis.desktop import youtube_page as yp
    from openjarvis.desktop.youtube_page import PageVideo

    asked: List[tuple] = []
    videos = [
        PageVideo("A", "aaaaaaaaaaa", W, (1, 1)),
        PageVideo("B", "bbbbbbbbbbb", W, (1, 2)),
    ]

    def fake_play(which: int, side: str):
        asked.append((which, side))
        return "Playing B (the 2nd video on the right).", videos, 1

    monkeypatch.setattr(yp, "play", fake_play)
    monkeypatch.setattr(server, "_mark_opening", lambda: None)
    monkeypatch.setattr(server.CONTEXT, "recent", Recent())
    monkeypatch.setattr(
        server.CONTEXT, "request", "open the 2nd video on the right side"
    )
    assert server.play_youtube(which=2, on_page=True).startswith("Playing B")
    assert asked == [(2, "right")]
    assert server.CONTEXT.recent.youtube_results[1] == ("bbbbbbbbbbb", "B")


@pytest.mark.parametrize(
    "said, allowed",
    [
        (
            "Jarvis, search for best phones under 40,000 with good camera "
            "quality and better battery in Amazon",
            False,  # a search, not "open five product tabs"
        ),
        ("open the Amazon page of the OnePlus Nord CE6 Lite", True),
        ("proceed to open the first three websites", True),
        ("Yes, please", True),  # after "Shall I open the pages?"
    ],
)
def test_pages_open_only_when_asked(said: str, allowed: bool) -> None:
    from openjarvis.desktop.graph import unrequested

    refusal = unrequested("open_page", {"count": 5}, said)
    assert (refusal is None) is allowed
    if refusal:
        assert "didn't ask to open web pages" in refusal
