"""Long-term desktop memory (SQLite + FTS5)."""

from __future__ import annotations

from pathlib import Path

from openjarvis.desktop.memory import MemoryStore


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory.sqlite")


def test_facts_are_recalled_by_meaning_words(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.remember("My favourite music is lofi hip hop")
    store.remember("My sister's name is Priya")
    hits = store.search("what kind of music do I like?")
    assert hits and hits[0].content == "My favourite music is lofi hip hop"
    assert "lofi" in store.context_for("play some music I like")


def test_duplicate_facts_are_stored_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.remember("I use Brave as my browser")
    assert store.remember("I use  Brave as my browser") == first
    assert len(store.all("fact")) == 1


def test_places_upsert_and_recall(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.remember_place("minecraft server", r"C:\old")
    store.remember_place("Minecraft  Server", r"C:\Downloads\Minecraft Server")
    assert store.recall_place("minecraft server") == r"C:\Downloads\Minecraft Server"
    assert store.recall_place("minecraft") == r"C:\Downloads\Minecraft Server"
    assert store.recall_place("photos") is None
    assert len(store.all("place")) == 1


def test_forget_and_clear(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.remember("My wifi password is in the drawer")
    gone = store.forget_matching("wifi password")
    assert gone and not store.search("wifi")
    store.remember("a", kind="episode")
    assert store.clear("episode") == 1


def test_episodes_are_capped(tmp_path: Path, monkeypatch) -> None:
    import openjarvis.desktop.memory as memory

    monkeypatch.setattr(memory, "_MAX_EPISODES", 3)
    store = _store(tmp_path)
    for i in range(6):
        store.remember(f"opened app {i}", kind="episode")
    assert [m.content for m in store.recent("episode")] == [
        "opened app 5",
        "opened app 4",
        "opened app 3",
    ]


def test_search_ignores_stop_words_and_odd_input(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.remember("Exam on Monday")
    assert store.search("the and of") == []
    assert store.search('"); DROP TABLE memories; --') == []
    assert store.search("exam")[0].content == "Exam on Monday"


def test_memory_survives_reopen(tmp_path: Path) -> None:
    _store(tmp_path).remember("I live in Hyderabad")
    assert (
        _store(tmp_path).search("where do I live")[0].content == "I live in Hyderabad"
    )
