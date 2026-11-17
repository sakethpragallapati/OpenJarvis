"""Your own YouTube Music playlists, played from the app's side panel."""

from __future__ import annotations

import time as real_time
from types import SimpleNamespace
from typing import List

import pytest

from openjarvis.desktop import server, ytmusic
from openjarvis.desktop.fastpath import FastIntent, match
from openjarvis.desktop.recent import Recent
from openjarvis.desktop.ytmusic import Playlist

MINE = [
    Playlist("Liked Music", "Auto playlist"),
    Playlist("Anime music", "SASRPLAYS"),
    Playlist("Phonk", "SASRPLAYS"),
    Playlist("Best Telugu Love Songs", "Aditya Music"),
    Playlist("English songs", "SASRPLAYS"),
    Playlist("Episodes for Later", "Auto playlist"),
]


@pytest.mark.parametrize(
    "query, title",
    [
        ("my anime music playlist", "Anime music"),
        ("My Anime Music Playlist", "Anime music"),
        ("my anime playlist", "Anime music"),
        ("my phonk playlist", "Phonk"),
        ("my liked songs", "Liked Music"),
        ("my english songs playlist", "English songs"),
        ("my telugu love songs playlist", "Best Telugu Love Songs"),
        ("my workout playlist", None),
        ("my heart will go on", None),
    ],
)
def test_the_playlist_you_named(query: str, title: str | None) -> None:
    found = ytmusic.match(MINE, query)
    assert (found.title if found else None) == title


@pytest.mark.parametrize(
    "query, own",
    [
        ("my anime music playlist", True),
        ("my liked songs", True),
        ("play my phonk playlist", True),
        ("my heart will go on", False),  # a song
        ("anime songs mix", False),
    ],
)
def test_what_counts_as_your_own_playlist(query: str, own: bool) -> None:
    assert ytmusic.asks_for_own(query) is own


def test_the_spoken_request_keeps_my() -> None:
    found = match(
        "Jarvis, Open YouTube Music and Play, My Anime Music Playlist", recent=Recent()
    )
    assert isinstance(found, FastIntent) and found.tool == "play_youtube"
    assert ytmusic.asks_for_own(found.args["query"])


def _app(monkeypatch, states: List[str], playing: List[bool]) -> List[str]:
    """ytmusic.play against a scripted side panel; returns pressed titles."""
    window = SimpleNamespace(hwnd=5)
    pressed: List[str] = []
    monkeypatch.setattr(ytmusic, "_app_window", lambda: window)
    monkeypatch.setattr(ytmusic, "playlists", lambda w: list(MINE))
    monkeypatch.setattr(ytmusic, "COM", SimpleNamespace(run=lambda fn, timeout=0: fn()))
    state = iter(states)
    monkeypatch.setattr(ytmusic, "_button_state", lambda hwnd, title: next(state))
    play = iter(playing)
    monkeypatch.setattr(ytmusic, "_is_playing", lambda hwnd: next(play))
    monkeypatch.setattr(
        ytmusic, "_click_play", lambda window, title: pressed.append(title) or True
    )
    clock = SimpleNamespace(sleep=lambda s: None, monotonic=real_time.monotonic)
    monkeypatch.setattr(ytmusic, "time", clock)
    return pressed


def test_your_playlist_is_played_not_a_search_result(monkeypatch) -> None:
    pressed = _app(monkeypatch, ["Play", "Pause"], [False, True])
    reply, title = ytmusic.play("my anime music playlist")
    assert pressed == ["Anime music"] and title == "Anime music"
    assert reply == "Playing your Anime music playlist in YouTube Music."


def test_a_playing_playlist_is_not_paused_by_pressing_it_again(monkeypatch) -> None:
    pressed = _app(monkeypatch, ["Pause"], [True])
    reply, _ = ytmusic.play("my anime music playlist")
    assert pressed == [] and reply == "Your Anime music playlist is already playing."


def test_success_is_not_claimed_from_music_that_was_already_on(monkeypatch) -> None:
    # Something else was playing, and the playlist's button never changed.
    _app(monkeypatch, ["Play"] + ["Play"] * 50, [True] + [True] * 50)
    ticks = iter(range(1000))
    ytmusic.time.monotonic = lambda: next(ticks)
    reply, _ = ytmusic.play("my anime music playlist")
    assert reply.startswith("I pressed Play") and "can't tell" in reply


def test_an_unknown_playlist_lists_yours_and_asks(monkeypatch) -> None:
    pressed = _app(monkeypatch, [], [])
    with pytest.raises(ytmusic.NoSuchPlaylist) as error:
        ytmusic.play("my workout playlist")
    message = str(error.value)
    assert "'workout'" in message and "Anime music" in message
    assert message.endswith("Say which.") and pressed == []


def _routing(monkeypatch, outcome) -> List[str]:
    from openjarvis.desktop import web

    searched: List[str] = []
    monkeypatch.setattr(ytmusic, "play", outcome)
    monkeypatch.setattr(
        web, "youtube_results", lambda q, n: searched.append(q) or [("v1", "Song")]
    )
    monkeypatch.setattr(web, "open_in_web_app", lambda name, url: True)
    monkeypatch.setattr(server, "_mark_opening", lambda: None)
    monkeypatch.setattr(server.CONTEXT, "recent", Recent())
    return searched


def test_play_tool_plays_your_playlist_and_remembers_it(monkeypatch) -> None:
    searched = _routing(
        monkeypatch,
        lambda q: ("Playing your Phonk playlist in YouTube Music.", "Phonk"),
    )
    reply = server.play_youtube("my phonk playlist", where="youtube_music")
    assert reply == "Playing your Phonk playlist in YouTube Music." and searched == []
    recent = server.CONTEXT.recent
    assert recent.youtube_where == "youtube_music"
    assert recent.youtube_results == [("", "Phonk")]


def test_a_song_starting_with_my_is_still_searched(monkeypatch) -> None:
    def none(query):
        raise ytmusic.NoSuchPlaylist("no such playlist")

    searched = _routing(monkeypatch, none)
    reply = server.play_youtube("my heart will go on", where="youtube_music")
    assert searched == ["my heart will go on"] and reply.startswith("Playing Song")


def test_a_missing_playlist_you_asked_for_is_never_swapped_for_a_search(
    monkeypatch,
) -> None:
    def none(query):
        raise ytmusic.NoSuchPlaylist("You don't have a playlist called 'workout'.")

    searched = _routing(monkeypatch, none)
    with pytest.raises(ytmusic.NoSuchPlaylist):
        server.play_youtube("my workout playlist", where="youtube_music")
    assert searched == []


def test_next_during_your_playlist_is_its_next_song(monkeypatch) -> None:
    from openjarvis.pc import control

    keys: List[str] = []
    monkeypatch.setattr(control, "media_key", lambda key: keys.append(key) or "Next.")
    recent = Recent()
    recent.played("Phonk", [("", "Phonk")], 0, where="youtube_music")
    monkeypatch.setattr(server.CONTEXT, "recent", recent)
    assert server.play_youtube("", which=0) == "Next."
    assert keys == ["next"]
