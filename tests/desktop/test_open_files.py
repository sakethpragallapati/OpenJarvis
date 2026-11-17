"""Opening files by what you say, not their exact names.

Files are looked up through the MCP filesystem server
(``@modelcontextprotocol/server-filesystem``); these tests drive the
lookup with a stand-in server, and one test runs the real one when it's
installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from openjarvis.desktop import files_mcp, places
from openjarvis.desktop.recent import Recent, listing_paths

NAMES = [
    "CC Mid-1 Notes.docx.pdf",
    "CC Mid-2 Notes.docx.pdf",
    "Bytebytego_Big_Archive_System_Design_2025.pdf",
    "Hands-On Large Language Models Language Understanding and Generation "
    "(Jay Alammar, Maarten Grootendorst) (Z-Library).pdf",
]


@pytest.fixture
def desk() -> Path:
    """Names as they'd be on your Desktop (scoring needs no real files)."""
    return Path.home() / "Desktop"


def _listing() -> str:
    """What fs_list_directory says about that Desktop."""
    return "\n".join(["[DIR] Nimbus"] + [f"[FILE] {n}" for n in NAMES])


def _best(query: str, desk: Path) -> str:
    scored = sorted((places._score(query, str(desk / n), False), n) for n in NAMES)
    return scored[-1][1] if scored[-1][0] >= 0.9 else ""


@pytest.mark.parametrize(
    "said, name",
    [
        ("CC mid1 notes", "CC Mid-1 Notes.docx.pdf"),  # "mid1" = "Mid-1"
        ("cc mid one notes", "CC Mid-1 Notes.docx.pdf"),  # number words
        ("cc mid 2 notes", "CC Mid-2 Notes.docx.pdf"),
        ("byte byte go", "Bytebytego_Big_Archive_System_Design_2025.pdf"),
        ("bite bite go system design", "Bytebytego_Big_Archive_System_Design_2025.pdf"),
        ("hands on large language models jay alamar", NAMES[3]),  # misheard
        ("cc mid three notes", ""),  # no Mid-3: never a confident wrong file
    ],
)
def test_spoken_names_find_the_file(desk: Path, said: str, name: str) -> None:
    assert _best(said, desk) == name


def test_numbers_must_match(desk: Path) -> None:
    one = places._score("CC mid1 notes", str(desk / NAMES[0]), False)
    two = places._score("CC mid1 notes", str(desk / NAMES[1]), False)
    assert one >= 0.9 > two  # "1notes" is not "notes"


def test_the_folder_just_listed_is_remembered(desk: Path) -> None:
    recent = Recent()
    recent.note("fs_list_directory", {"path": str(desk)}, _listing(), True)
    assert len(recent.paths) == len(NAMES) + 1
    assert listing_paths(str(desk), _listing())[0].endswith("Nimbus")
    best = places.best_listed("CC mid1 notes", recent.paths)
    assert best is not None and best.endswith("CC Mid-1 Notes.docx.pdf")
    assert places.best_listed("cc notes", recent.paths) is None  # two equally good


def test_open_uses_the_listed_file_without_searching(desk: Path, monkeypatch) -> None:
    from openjarvis.desktop import server

    recent = Recent()
    recent.note("fs_list_directory", {"path": str(desk)}, _listing(), True)
    monkeypatch.setattr(server.CONTEXT, "recent", recent)
    opened: List[str] = []
    monkeypatch.setattr(server.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(server, "_mark_opening", lambda: None)
    monkeypatch.setattr(
        places, "resolve", lambda *a, **k: pytest.fail("searched instead")
    )
    reply = server.open_target("CC mid1 notes")
    assert opened == [str(desk / "CC Mid-1 Notes.docx.pdf")]
    assert reply.startswith("Opened CC Mid-1 Notes.docx.pdf")


def test_unsure_matches_ask_instead_of_guessing(monkeypatch) -> None:
    from openjarvis.desktop import server
    from openjarvis.desktop.winapi import DesktopError

    monkeypatch.setattr(server.CONTEXT, "recent", Recent())
    monkeypatch.setattr(places, "_FILES", None)
    found = SimpleNamespace(learned=True, confident=False, kind="file", target="x")
    monkeypatch.setattr(places, "resolve", lambda *a, **k: found)
    hits = [SimpleNamespace(path=r"C:\d\CC Mid-1 Notes.docx.pdf"),
            SimpleNamespace(path=r"C:\d\CC Mid-2 Notes.docx.pdf")]  # fmt: skip
    monkeypatch.setattr(places, "search", lambda *a, **k: hits)
    with pytest.raises(DesktopError) as error:
        server.open_target("cc mid three notes")
    message = str(error.value)
    assert message.endswith("Say which.")  # the turn stops and asks you
    assert "CC Mid-1 Notes.docx.pdf" in message and "CC Mid-2 Notes.docx.pdf" in message


@pytest.mark.parametrize(
    "said, target",
    [
        ("the second one", "CC Mid-2 Notes.docx.pdf"),
        ("cc mid 2", "CC Mid-2 Notes.docx.pdf"),
        ("Yes, please", None),  # two choices: yes doesn't say which
    ],
)
def test_answering_which_file(said: str, target) -> None:
    from openjarvis.desktop.fastpath import match

    recent = Recent()
    recent.note(
        "open",
        {"target": "cc mid three notes"},
        "I couldn't find 'cc mid three notes' exactly. Did you mean CC Mid-1 "
        "Notes.docx.pdf or CC Mid-2 Notes.docx.pdf? Say which.",
        False,
    )
    found = match(said, recent=recent, tools={"open"}, can_open=lambda t: False)
    assert (found.args["target"] if found else None) == target


def test_tools_never_act_on_jarviss_own_window() -> None:
    from openjarvis.desktop import winapi

    own = winapi.WindowInfo(1, "J.A.R.V.I.S.", "pythonw.exe", os.getpid())
    other = winapi.WindowInfo(2, "CC Mid-1 Notes.docx.pdf - Adobe", "Acrobat.exe", 4)
    assert winapi.is_terminal(own) and not winapi.is_terminal(other)


# ---------------------------------------------------------------------------
# Misheard names, "in desktop", and never clicking around for files
# ---------------------------------------------------------------------------


@pytest.fixture
def no_launch(monkeypatch) -> List[str]:
    """Nothing is really opened, remembered or searched for as an app."""
    from openjarvis.desktop import server

    opened: List[str] = []
    monkeypatch.setattr(server.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(server, "_launch", lambda found: opened.append(found.target))
    monkeypatch.setattr(server, "_mark_opening", lambda: None)
    monkeypatch.setattr(server.CONTEXT, "recent", Recent())
    monkeypatch.setattr(
        server.CONTEXT,
        "memory",
        SimpleNamespace(recall_place=lambda n: None, remember_place=lambda n, t: None),
    )
    monkeypatch.setattr("openjarvis.pc.control.find_app", lambda name: None)
    return opened


def _desk_paths(desk: Path) -> List[str]:
    return [str(desk / n) for n in NAMES] + [str(desk / "Nimbus")]


@pytest.mark.parametrize(
    "said, name",
    [
        ("CC mid-1 codes", "CC Mid-1 Notes.docx.pdf"),  # "codes" for "notes"
        ("bite bite go", "Bytebytego_Big_Archive_System_Design_2025.pdf"),
        ("cc mid one notes", "CC Mid-1 Notes.docx.pdf"),
    ],
)
def test_misheard_names_are_matched_by_sound(desk: Path, said: str, name: str) -> None:
    best, close = places.sound_pick(said, _desk_paths(desk))
    assert best is not None and best.endswith(name) and close == []


def test_unclear_names_ask_and_unrelated_ones_dont_match(desk: Path) -> None:
    best, close = places.sound_pick("CC Midland Hodes", _desk_paths(desk))
    assert best is None  # Mid-1 or Mid-2? It can't tell.
    assert {Path(p).name for p in close} == {NAMES[0], NAMES[1]}
    assert places.sound_pick("weather report", _desk_paths(desk)) == (None, [])


def test_a_file_is_never_clicked_open() -> None:
    from openjarvis.desktop.graph import file_by_clicking

    refusal = file_by_clicking("ui", {"action": "click", "name": NAMES[0]})
    assert refusal and "call open" in refusal
    assert file_by_clicking("ui", {"action": "click", "name": "Send"}) is None


def test_nothing_is_clicked_or_typed_into_jarviss_own_window(monkeypatch) -> None:
    from openjarvis.desktop import server, winapi
    from openjarvis.desktop.winapi import DesktopError

    own = winapi.WindowInfo(1, "J.A.R.V.I.S.", "pythonw.exe", os.getpid())
    monkeypatch.setattr(winapi, "foreground_window", lambda: own)
    monkeypatch.setattr(winapi, "app_window", lambda: None)  # nothing else open
    with pytest.raises(DesktopError, match="only Jarvis's own window"):
        server._bring_app_forward(require=True)
    server._bring_app_forward(require=False)  # "describe the screen" is fine


# ---------------------------------------------------------------------------
# The lookup through the filesystem server (a stand-in server)
# ---------------------------------------------------------------------------


class _Server:
    """Answers like the MCP filesystem server for a few folders."""

    def __init__(self, tree: Dict[str, Any]) -> None:
        self.tree = tree  # {root: [{"name", "type", "children"}]}
        self.calls: List[tuple] = []

    @staticmethod
    def _result(text: str, error: bool = False) -> Any:
        return SimpleNamespace(content=[SimpleNamespace(text=text)], is_error=error)

    def _nodes(self, path: str) -> List[Dict[str, Any]]:
        for root, nodes in self.tree.items():
            if path.lower() == root.lower():
                return nodes
        return []

    async def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        self.calls.append((name, args))
        if name == "fs_list_directory":
            nodes = self._nodes(args["path"])
            tags = {"directory": "[DIR]", "file": "[FILE]"}
            return self._result(
                "\n".join(f"{tags[n['type']]} {n['name']}" for n in nodes)
            )
        if name == "fs_directory_tree":

            def prune(nodes: List[Dict[str, Any]], depth: int) -> List[Dict[str, Any]]:
                out = []
                for n in nodes:
                    n = dict(n)
                    if "children" in n:
                        n["children"] = (
                            prune(n["children"], depth - 1) if depth > 1 else []
                        )
                    out.append(n)
                return out

            nodes = prune(self._nodes(args["path"]), int(args.get("depth") or 99))
            return self._result(json.dumps(nodes))
        if name == "fs_search_files":
            word = re.sub(r"\[(.).\]", r"\1", args["pattern"]).strip("*/")
            found = [
                path
                for path, _ in files_mcp._flatten(
                    args["path"], self._nodes(args["path"])
                )
                if word.lower() in Path(path).name.lower()
            ]
            return self._result("\n".join(found) or "No matches found")
        return self._result(f"unknown tool {name}", error=True)


@pytest.fixture
def loop():
    """An event loop on its own thread, like the agent's ToolRuntime."""
    running = asyncio.new_event_loop()
    thread = threading.Thread(target=running.run_forever, daemon=True)
    thread.start()
    yield running
    running.call_soon_threadsafe(running.stop)


def _store(desk: Path, loop, monkeypatch) -> tuple:
    deep = {
        "name": "Games",
        "type": "directory",
        "children": [
            {
                "name": "Servers",
                "type": "directory",
                "children": [
                    {"name": "Minecraft Server", "type": "directory", "children": []}
                ],
            }
        ],
    }
    nodes = [{"name": n, "type": "file"} for n in NAMES]
    nodes += [{"name": "Nimbus", "type": "directory", "children": []}, deep]
    fake = _Server({str(desk): nodes})
    store = files_mcp.FileStore(fake, [str(desk)])
    store.loop = loop
    monkeypatch.setattr(places, "_FILES", store)
    return store, fake


def test_open_looks_files_up_through_the_server(desk, loop, monkeypatch, no_launch):
    from openjarvis.desktop import server

    store, fake = _store(desk, loop, monkeypatch)
    reply = server.open_target("cc mid one notes")
    assert reply.startswith("Opened CC Mid-1 Notes.docx.pdf")
    assert no_launch == [str(desk / NAMES[0])]
    # Two levels of Desktop were enough: no slow search.
    assert [c[0] for c in fake.calls] == ["fs_directory_tree"]


def test_deeper_files_are_searched_for_by_name(desk, loop, monkeypatch) -> None:
    store, fake = _store(desk, loop, monkeypatch)
    hits = places.search("minecraft server")  # (stand-in paths aren't on disk)
    assert hits and hits[0].path.endswith(r"Games\Servers\Minecraft Server")
    search = [args for name, args in fake.calls if name == "fs_search_files"]
    assert (
        search
        and search[0]["pattern"]
        == "**/*" + "".join(f"[{c}{c.upper()}]" for c in "minecraft") + "*"
    )
    assert "**/node_modules/**" in search[0]["excludePatterns"]


def test_a_named_folder_is_listed_directly(desk, loop, monkeypatch, no_launch) -> None:
    from openjarvis.desktop import server
    from openjarvis.desktop.winapi import DesktopError

    name, folder = places.scope_folder("CC, Midland Hodes in my desktop")
    assert name == "CC, Midland Hodes" and folder is not None
    assert folder.name.lower() == "desktop"
    store, fake = _store(folder, loop, monkeypatch)
    monkeypatch.setattr(places, "resolve", lambda *a, **k: pytest.fail("searched"))
    assert server.open_target("CC mid-1 codes in desktop").startswith(
        "Opened CC Mid-1 Notes.docx.pdf"
    )
    assert fake.calls[0] == ("fs_list_directory", {"path": str(folder)})
    with pytest.raises(DesktopError, match="Say which"):
        server.open_target("CC Midland Hodes in desktop")
    with pytest.raises(DesktopError, match="nothing like 'weather report'"):
        server.open_target("weather report in desktop")


def test_listings_trees_and_patterns_are_read() -> None:
    sized = "[FILE] a b.pdf               3 B\n[DIR] Sub Folder   \n\nTotal: 1 files"
    assert files_mcp.listing("C:\\d", sized) == [
        ("C:\\d\\a b.pdf", False),
        ("C:\\d\\Sub Folder", True),
    ]
    leaf = {"name": "y.txt", "type": "file"}
    tree = [{"name": "x", "type": "directory", "children": [leaf]}]
    assert files_mcp._flatten("C:\\d\\", tree) == [
        ("C:\\d\\x", True),
        ("C:\\d\\x\\y.txt", False),
    ]
    assert files_mcp._glob_word("Mid1") == "**/*[mM][iI][dD]1*"


def test_the_server_is_started_from_jarviss_own_copy(tmp_path, monkeypatch) -> None:
    script = tmp_path / "index.js"
    script.write_text("")
    monkeypatch.setattr(files_mcp, "entry_script", lambda base=None: script)
    monkeypatch.setattr(files_mcp, "_bundled_node", lambda: None)
    monkeypatch.setattr(files_mcp.shutil, "which", lambda name: f"C:\\bin\\{name}")
    config = files_mcp.server_config(["C:\\Users\\me\\Desktop"])
    assert config == {
        "command": "C:\\bin\\node.exe",
        "args": [str(script), "--confine-roots", "--", "C:\\Users\\me\\Desktop"],
    }
    # The installer's own Node.js wins over an older one on PATH.
    bundled = tmp_path / "OpenJarvis" / "node"
    bundled.mkdir(parents=True)
    (bundled / "node.exe").write_text("")
    monkeypatch.setattr(files_mcp, "_bundled_node", lambda: bundled)
    assert files_mcp.server_config(["C:\\d"])["command"] == str(bundled / "node.exe")
    monkeypatch.setattr(files_mcp, "_bundled_node", lambda: None)
    monkeypatch.setattr(files_mcp, "entry_script", lambda base=None: None)
    monkeypatch.setattr(files_mcp.shutil, "which", lambda name: None)
    assert files_mcp.server_config(["C:\\Users\\me\\Desktop"]) is None  # no Node.js
    assert files_mcp.server_config([]) is None


def test_extra_folders_are_allowed(tmp_path) -> None:
    games = tmp_path / "Games"
    games.mkdir()
    folders = files_mcp.user_folders(f"{games};{tmp_path / 'missing'}")
    assert str(games) in folders
    assert not any(f.endswith("missing") for f in folders)


def test_hidden_tools_are_never_offered(monkeypatch) -> None:
    from openjarvis.desktop import server
    from openjarvis.desktop.runtime import ToolRuntime

    runtime = ToolRuntime(server.build_server(files=False), hidden={"system"})
    try:
        assert "system" not in runtime.names
        assert all(s["function"]["name"] != "system" for s in runtime.schemas)
        assert not runtime.call("system", {"action": "time"}).ok
        assert {"recycle", "open"} <= set(runtime.names)
        assert not {"find_files", "files"} & set(runtime.names)  # the old ones
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# The real server (when it's installed)
# ---------------------------------------------------------------------------


# Tests run with a throwaway ~/.openjarvis, so look where Jarvis installs it.
_REAL = files_mcp.entry_script(
    Path.home() / ".openjarvis" / "mcp" / "server-filesystem"
)


@pytest.mark.skipif(
    _REAL is None or shutil.which("node") is None,
    reason="the MCP filesystem server isn't installed",
)
def test_the_real_filesystem_server(tmp_path, monkeypatch, no_launch) -> None:
    from openjarvis.desktop import server
    from openjarvis.desktop.runtime import ToolRuntime

    for name in NAMES[:2]:
        (tmp_path / name).write_text("x")
    (tmp_path / "Nimbus").mkdir()
    monkeypatch.setattr(files_mcp, "entry_script", lambda base=None: _REAL)
    monkeypatch.setattr(files_mcp, "user_folders", lambda extra="": [str(tmp_path)])
    monkeypatch.setattr(places, "_FILES", None)
    # pytest's folders live under AppData, which scores lower on purpose.
    monkeypatch.setattr(places, "_SKIP_PARTS", places._SKIP_PARTS - {"appdata"})
    runtime = ToolRuntime(
        server.build_server(gmail=False), hidden=files_mcp.hidden_names()
    )
    try:
        names = set(runtime.names)
        assert {
            "fs_list_directory",
            "fs_search_files",
            "fs_read_text_file",
            "fs_write_file",
            "fs_move_file",
            "fs_copy_file",
        } <= names
        assert not names & files_mcp.hidden_names()
        places.files().loop = runtime.loop
        # Inside a tool call: open asks the server through AnyIO.
        out = runtime.call("open", {"target": "cc mid one notes"})
        assert out.ok and out.text.startswith("Opened CC Mid-1 Notes.docx.pdf"), (
            out.text
        )
        assert no_launch == [str(tmp_path / NAMES[0])]
        # From the agent's own thread: through the runtime's loop.
        hits = places.search("nimbus")
        assert hits and hits[0].path.endswith("Nimbus")
        note = tmp_path / "note.txt"
        assert runtime.call("fs_write_file", {"path": str(note), "content": "hi"}).ok
        assert runtime.call("fs_read_text_file", {"path": str(note)}).text == "hi"
        windows = os.environ.get("SystemRoot", "C:\\Windows")
        outside = runtime.call("fs_list_directory", {"path": windows})
        assert not outside.ok and "outside allowed" in outside.text
    finally:
        runtime.close()
        places.use_files(None)
